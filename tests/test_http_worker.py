"""Офлайн-тесты парсера и утилит http_worker на реальных HTML-дампах из ./data.

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import Account, Config
from app.http_worker import (
    SITE_HOST,
    Metrics,
    PayoutClient,
    _classify,
    load_cookies,
    parse_rows,
    take_loop,
)
from app.state import state

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# HTML-дампы лежат в ./data, который в .gitignore — тесты, которым нужны дампы,
# помечены skipUnless и в чистом клоне просто пропускаются.
DUMP_ROWS = DATA / "order_1790622899_rows.html"
DUMP_FULL = DATA / "order_1790611560_full_page.html"
DUMP_MULTI = DATA / "order_1790609721_rows.html"


def make_config(**overrides) -> Config:
    """Config для тестов. site_login/site_password/storage_state_path — удобные
    ключи: они подставляются в кабинет #1 (Config хранит список accounts)."""
    site_login = overrides.pop("site_login", "a@b.c")
    site_password = overrides.pop("site_password", "p")
    storage_state = overrides.pop("storage_state_path", str(DATA / "auth.json"))
    base = dict(
        telegram_token="t",
        allowed_chat_ids=[1],
        accounts=[
            Account(
                index=1,
                name="Кабинет 1",
                login=site_login,
                password=site_password,
                storage_state_path=storage_state,
            )
        ],
        site_url=f"https://{SITE_HOST}/signin",
        queue_url=f"https://{SITE_HOST}/account/payouts/?sub=payouts_choice",
        demands_url=f"https://{SITE_HOST}/account/payouts/?sub=demands",
        target_queue_size=10,
        reload_min_ms=1500,
        reload_max_ms=2000,
        take_poll_min_ms=500,
        take_poll_max_ms=900,
        debug_capture_attempts=True,
        headless=False,
        log_path=str(DATA / "actions.log"),
        http_timeout_s=10.0,
        metrics_path=str(DATA / "metrics.jsonl"),
        worker_backend="http",
        take_parallel_pollers=1,
    )
    base.update(overrides)
    return Config(**base)


class ParseRowsTest(unittest.TestCase):
    @unittest.skipUnless(DUMP_ROWS.exists(), "нет HTML-дампов в ./data")
    def test_parses_real_rows_dump(self):
        html = DUMP_ROWS.read_text(encoding="utf-8")
        rows = parse_rows(html)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "6408416")
        self.assertIn("провайдера", row["status"].lower())
        self.assertEqual(row["csrf_token"], "f2ea90d03d015d5a20693b8f164c02d49ab0bfc315ecca3ef21f0ae07e67f8bc")
        self.assertEqual(row["access_key"], "")

    @unittest.skipUnless(DUMP_FULL.exists(), "нет HTML-дампов в ./data")
    def test_parses_full_page_dump(self):
        html = DUMP_FULL.read_text(encoding="utf-8")
        rows = parse_rows(html)
        self.assertEqual([r["id"] for r in rows], ["6407843"])
        self.assertTrue(rows[0]["csrf_token"])

    @unittest.skipUnless(DUMP_MULTI.exists(), "нет HTML-дампов в ./data")
    def test_parses_multi_row_dump(self):
        # в одном из дампов 2+ строк — убеждаемся, что берём все заявки
        html = DUMP_MULTI.read_text(encoding="utf-8")
        rows = parse_rows(html)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(r["id"].isdigit() for r in rows))

    def test_skips_stub_row(self):
        html = (
            "<table class='payout-table'><tbody>"
            "<tr><td class='payout-status'>Не найдено</td></tr>"
            "</tbody></table>"
        )
        self.assertEqual(parse_rows(html), [])

    def test_ignores_unrelated_tables(self):
        html = (
            "<table><tbody><tr><td>1</td><td>2</td><td>3</td></tr></tbody></table>"
            "<table class='payout-table'><tbody><tr>"
            "<td class='payout-status payout-status--waiting-provider'>"
            "<b># 777</b><br><span class='badge badge-primary'>Ждет провайдера</span></td>"
            "<td><form><input type='hidden' name='csrf_token' value='abc'>"
            "<input type='hidden' name='act' value='take_payout'>"
            "<input type='hidden' name='payout_id' value='777'>"
            "<input type='hidden' name='access_key' value=''></form></td>"
            "</tr></tbody></table>"
        )
        rows = parse_rows(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "777")
        self.assertEqual(rows[0]["csrf_token"], "abc")


class ClassifyTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(_classify("<html><body>payouts</body></html>", "https://x/account/payouts/"), "ok")

    def test_signin_by_url(self):
        self.assertEqual(_classify("<html></html>", "https://x/signin"), "signin")

    def test_signin_by_markup(self):
        self.assertEqual(_classify('<form class="auth-form"></form>', "https://x/"), "signin")

    def test_blocked(self):
        self.assertEqual(_classify('script src="https://geo.captcha-delivery.com/c.js"', "https://x/"), "blocked")


class LoadCookiesTest(unittest.TestCase):
    def test_filters_by_domain(self):
        state = {
            "cookies": [
                {"name": "PHPSESSID", "value": "sess", "domain": "spark-trading.cash"},
                {"name": "__ddg1_", "value": "dd", "domain": ".spark-trading.cash"},
                {"name": "other", "value": "x", "domain": "example.com"},
            ],
            "origins": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text(json.dumps(state), encoding="utf-8")
            cookies = load_cookies(make_config(storage_state_path=str(p)).accounts[0])
        self.assertEqual(cookies, {"PHPSESSID": "sess", "__ddg1_": "dd"})

    def test_missing_file(self):
        cookies = load_cookies(make_config(storage_state_path="/nonexistent/auth.json").accounts[0])
        self.assertEqual(cookies, {})


class TakeLoopEndToEndTest(unittest.TestCase):
    """Полный путь горячего цикла на локальном HTTP-сервере: GET очереди ->
    разбор HTML -> POST take_payout -> запись метрики saw_to_sent_ms.
    Отличается от реального сайта только адресом (127.0.0.1 вместо spark-trading.cash)."""

    @unittest.skipUnless(DUMP_ROWS.exists(), "нет HTML-дампов в ./data")
    def test_take_loop_posts_and_measures(self):
        row_html = DUMP_ROWS.read_text(encoding="utf-8")
        page_html = (
            "<!DOCTYPE html><html><body><table class='payout-table'><tbody>"
            + row_html
            + "</tbody></table></body></html>"
        )
        posted: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence test output
                pass

            def _send(self, body: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(page_html.encode("utf-8"))

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                posted.append(self.rfile.read(length).decode("utf-8"))
                self._send(page_html.encode("utf-8"))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}/account/payouts/"

        with tempfile.TemporaryDirectory() as tmp:
            metrics_path = Path(tmp) / "metrics.jsonl"
            cfg = make_config(
                queue_url=base + "?sub=payouts_choice",
                demands_url=base + "?sub=demands",
                metrics_path=str(metrics_path),
                debug_capture_attempts=False,
                target_queue_size=1,
                take_poll_min_ms=50,
                take_poll_max_ms=80,
            )

            ws = state.ensure(1, "Кабинет 1")
            ws.running = True
            ws.mode = "take"
            ws.taken_order_ids = set()
            ws.taken_count = 0

            async def notify(text: str):
                pass

            async def notify_file(path: str, caption: str = ""):
                pass

            async def run():
                client = PayoutClient(cfg, {})
                try:
                    task = asyncio.create_task(
                        take_loop(client, cfg, cfg.accounts[0], ws, notify, notify_file, Metrics(cfg))
                    )
                    await asyncio.sleep(0.8)
                    ws.running = False
                    await asyncio.wait_for(task, timeout=3)
                finally:
                    await client.aclose()

            try:
                asyncio.run(run())
            finally:
                ws.running = False
                server.shutdown()
                server.server_close()

            self.assertTrue(posted, "горячий цикл не отправил ни одного POST")
            self.assertTrue(any("act=take_payout" in d for d in posted))
            self.assertTrue(any("payout_id=6408416" in d for d in posted))
            self.assertTrue(any("csrf_token=f2ea90d0" in d for d in posted))

            self.assertTrue(metrics_path.exists(), "метрики не записаны")
            take_records = [
                json.loads(line)
                for line in metrics_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            take_records = [r for r in take_records if r.get("event") == "take"]
            self.assertTrue(take_records, "нет ни одной записи event=take")
            self.assertEqual(take_records[0]["order_id"], "6408416")
            for key in ("saw_to_sent_ms", "poll_rtt_ms", "parse_ms", "py_overhead_ms", "submit_rtt_ms"):
                self.assertIn(key, take_records[0])
                self.assertGreaterEqual(take_records[0][key], 0)


def _start_queue_server(page_html: str):
    """Мини-HTTP-сервер, всегда отдающий одну и ту же страницу очереди и
    записывающий тела POST-запросов. Возвращает (server, posted, port)."""
    posted: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _send(self, body: bytes):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(page_html.encode("utf-8"))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            posted.append(self.rfile.read(length).decode("utf-8"))
            self._send(page_html.encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, posted, server.server_address[1]


class ParallelPollersTest(unittest.TestCase):
    """Несколько воронок опроса не должны создавать дубликаты взятия одной заявки."""

    @unittest.skipUnless(DUMP_ROWS.exists(), "нет HTML-дампов в ./data")
    def test_parallel_pollers_do_not_duplicate_take(self):
        row_html = DUMP_ROWS.read_text(encoding="utf-8")
        page_html = (
            "<!DOCTYPE html><html><body><table class='payout-table'><tbody>"
            + row_html
            + "</tbody></table></body></html>"
        )
        server, posted, port = _start_queue_server(page_html)
        base = f"http://127.0.0.1:{port}/account/payouts/"

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                queue_url=base + "?sub=payouts_choice",
                demands_url=base + "?sub=demands",
                metrics_path=str(Path(tmp) / "metrics.jsonl"),
                debug_capture_attempts=False,
                target_queue_size=1,
                take_poll_min_ms=50,
                take_poll_max_ms=80,
                take_parallel_pollers=3,
            )
            ws = state.ensure(1, "Кабинет 1")
            ws.running = True
            ws.mode = "take"
            ws.taken_order_ids = set()
            ws.taken_count = 0

            async def notify(text: str):
                pass

            async def notify_file(path: str, caption: str = ""):
                pass

            async def run():
                client = PayoutClient(cfg, {})
                try:
                    task = asyncio.create_task(
                        take_loop(client, cfg, cfg.accounts[0], ws, notify, notify_file, Metrics(cfg))
                    )
                    await asyncio.sleep(0.8)
                    ws.running = False
                    await asyncio.wait_for(task, timeout=3)
                finally:
                    await client.aclose()

            try:
                asyncio.run(run())
            finally:
                ws.running = False
                server.shutdown()
                server.server_close()

            self.assertEqual(len(posted), 1, f"ожидался ровно 1 POST, получено {len(posted)}")
            self.assertIn("payout_id=6408416", posted[0])


if __name__ == "__main__":
    unittest.main()
