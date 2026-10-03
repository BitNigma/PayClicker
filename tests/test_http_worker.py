"""Офлайн-тесты парсера и утилит http_worker на реальных HTML-дампах из ./data.

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import Account, Config
from app.http_worker import (
    STATUS_IN_WORK_MARKER,
    SITE_HOST,
    Metrics,
    PayoutClient,
    _classify,
    in_work_ids,
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


def _row_html(order_id: str, status: str, with_form: bool = True, csrf: str = "csrf") -> str:
    """Строка таблицы выплат ровно в том виде, который разбирает parse_rows.

    with_form=True — строка очереди (есть форма take_payout); False — строка
    страницы demands (там формы нет, id берётся из текста «# 123»).
    """
    form = ""
    if with_form:
        form = (
            "<td><form>"
            f"<input type='hidden' name='csrf_token' value='{csrf}'>"
            "<input type='hidden' name='act' value='take_payout'>"
            f"<input type='hidden' name='payout_id' value='{order_id}'>"
            "<input type='hidden' name='access_key' value=''>"
            "</form></td>"
        )
    return (
        "<tr><td class='payout-status'>"
        f"<b># {order_id}</b><br><span class='badge'>{status}</span></td>"
        f"{form}</tr>"
    )


def _page_html(*rows: str) -> str:
    return (
        "<!DOCTYPE html><html><body><table class='payout-table'><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )


def _start_payout_server(queue_html, demands_html=None):
    """Локальный «сайт»: страница очереди и страница demands отдаются разными путями.

    demands_html может быть функцией без аргументов — тогда её содержимое можно
    менять по ходу теста (например, заявка завершилась и слот освободился).
    """
    posted: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _send(self, html: str):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _page(self) -> str:
            page = demands_html if "sub=demands" in self.path else queue_html
            if callable(page):
                page = page()
            return page if page is not None else queue_html

        def do_GET(self):
            self._send(self._page())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            posted.append(self.rfile.read(length).decode("utf-8"))
            self._send(self._page())

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


class TwoAccountsConcurrentlyTest(unittest.TestCase):
    """Два кабинета работают одновременно: у каждого свои cookies и свой опрос
    очереди, заявки не перепутываются между кабинетами."""

    def setUp(self):
        state.workers.clear()
        state.chat_pending.clear()

    @unittest.skipUnless(DUMP_ROWS.exists(), "нет HTML-дампов в ./data")
    def test_two_accounts_take_their_own_orders_in_parallel(self):
        row_html = DUMP_ROWS.read_text(encoding="utf-8")
        first_id, second_id = "6408416", "6408417"
        self.assertIn(first_id, row_html)
        page1 = (
            "<!DOCTYPE html><html><body><table class='payout-table'><tbody>"
            + row_html
            + "</tbody></table></body></html>"
        )
        # кабинет №2 видит свою заявку (другой id) на своём сервере
        page2 = page1.replace(first_id, second_id)

        server1, posted1, port1 = _start_queue_server(page1)
        server2, posted2, port2 = _start_queue_server(page2)
        base1 = f"http://127.0.0.1:{port1}/account/payouts/"
        base2 = f"http://127.0.0.1:{port2}/account/payouts/"

        with tempfile.TemporaryDirectory() as tmp:
            accounts = [
                Account(
                    index=1,
                    name="Кабинет 1",
                    login="a@b.c",
                    password="p",
                    storage_state_path=str(DATA / "auth.json"),
                ),
                Account(
                    index=2,
                    name="Кабинет 2",
                    login="d@e.f",
                    password="p",
                    storage_state_path=str(Path(tmp) / "auth_2.json"),
                ),
            ]
            cfg = make_config(
                accounts=accounts,
                debug_capture_attempts=False,
                target_queue_size=1,
                take_poll_min_ms=50,
                take_poll_max_ms=80,
                metrics_path=str(Path(tmp) / "metrics.jsonl"),
            )
            # у каждого кабинета свой сайт/очередь (в реальности — свои cookies)
            cfg1 = replace(cfg, queue_url=base1 + "?sub=payouts_choice", demands_url=base1 + "?sub=demands")
            cfg2 = replace(cfg, queue_url=base2 + "?sub=payouts_choice", demands_url=base2 + "?sub=demands")

            ws1 = state.ensure(1, "Кабинет 1")
            ws1.running, ws1.mode = True, "take"
            ws2 = state.ensure(2, "Кабинет 2")
            ws2.running, ws2.mode = True, "take"

            async def notify(text: str):
                pass

            async def notify_file(path: str, caption: str = ""):
                pass

            async def run():
                client1, client2 = PayoutClient(cfg1, {}), PayoutClient(cfg2, {})
                task1 = asyncio.create_task(
                    take_loop(client1, cfg1, accounts[0], ws1, notify, notify_file, Metrics(cfg1))
                )
                task2 = asyncio.create_task(
                    take_loop(client2, cfg2, accounts[1], ws2, notify, notify_file, Metrics(cfg2))
                )
                await asyncio.sleep(0.8)
                ws1.running = ws2.running = False
                await asyncio.wait_for(asyncio.gather(task1, task2), timeout=3)
                await client1.aclose()
                await client2.aclose()

            try:
                asyncio.run(run())
            finally:
                ws1.running = ws2.running = False
                for server in (server1, server2):
                    server.shutdown()
                    server.server_close()

            self.assertEqual(len(posted1), 1, f"кабинет 1: ожидался 1 POST, получено {len(posted1)}")
            self.assertEqual(len(posted2), 1, f"кабинет 2: ожидался 1 POST, получено {len(posted2)}")
            self.assertIn(f"payout_id={first_id}", posted1[0])
            self.assertIn(f"payout_id={second_id}", posted2[0])
            self.assertEqual(ws1.taken_order_ids, {first_id})
            self.assertEqual(ws2.taken_order_ids, {second_id})
            self.assertEqual((ws1.taken_count, ws2.taken_count), (1, 1))


class DemandsSlotAccountingTest(unittest.TestCase):
    """Размер активной очереди считается по площадке (страница demands), а не по памяти.

    Так закрываются две дыры прежней логики: (1) после перезапуска процесса память
    пуста и бот донабирает лишние заявки; (2) заявки, которые площадка выдала сама
    («автовыдача»), занимают слот целевой очереди, хотя в памяти бота их нет вовсе.
    """

    def setUp(self):
        state.workers.clear()
        state.chat_pending.clear()

    @staticmethod
    def _collector():
        texts: list[str] = []

        async def notify(text: str):
            texts.append(text)

        async def notify_file(path: str, caption: str = ""):
            pass

        return texts, notify, notify_file

    def _run_take_loop(self, cfg, ws, notify, notify_file, seconds: float, mid=None, mid_wait=None):
        """Гоняет take_loop `seconds` секунд; mid — корутина, меняющая состояние сайта.

        mid_wait — сколько ждать ПОСЛЕ mid (по умолчанию столько же, сколько до него):
        нужно, чтобы цикл сверки успел заметить изменение страницы demands.
        """

        async def run():
            client = PayoutClient(cfg, {})
            try:
                task = asyncio.create_task(
                    take_loop(client, cfg, cfg.accounts[0], ws, notify, notify_file, Metrics(cfg))
                )
                await asyncio.sleep(seconds)
                if mid is not None:
                    await mid()
                    await asyncio.sleep(mid_wait if mid_wait is not None else seconds)
                ws.running = False
                await asyncio.wait_for(task, timeout=5)
            finally:
                await client.aclose()

        asyncio.run(run())

    def test_in_work_ids_keeps_only_working_orders(self):
        rows = parse_rows(
            _page_html(
                _row_html("9001", "В работе у провайдера", with_form=False),
                _row_html("9002", "Выполнена", with_form=False),
                _row_html("9003", "Ждет провайдера", with_form=False),
                _row_html("9004", "В работе у провайдера (проверяется)", with_form=False),
            )
        )
        # слот занимают только заявки, которые прямо сейчас выполняются
        self.assertEqual(in_work_ids(rows), {"9001", "9004"})
        self.assertIn(STATUS_IN_WORK_MARKER, "в работе у провайдера".lower())

    def test_slots_taken_unions_platform_memory_and_inflight(self):
        ws = state.ensure(1, "Кабинет 1")
        ws.demands_ids = {"10", "11"}      # в работе на площадке
        ws.taken_order_ids = {"11", "12"}  # взяли мы (11 — пересечение, дубль не считаем)
        self.assertEqual(ws.slots_taken(), 3)
        self.assertEqual(ws.slots_taken({"13", "10"}), 4)  # + отправленное, но не подтверждённое

    def test_preflight_sync_counts_orders_already_in_work(self):
        """Перед стартом бот спрашивает площадку: часть заявок уже в работе (в т.ч. автовыдача)."""
        queue = _page_html(_row_html("5001", "Ждет провайдера"))
        demands = _page_html(
            _row_html("9001", "В работе у провайдера", with_form=False),
            _row_html("9002", "В работе у провайдера", with_form=False),
            _row_html("9003", "В работе у провайдера", with_form=False),
            # взятая нами заявка видна на demands — подтверждение срабатывает сразу
            _row_html("5001", "Ждет провайдера", with_form=False),
        )
        server, posted, port = _start_payout_server(queue, demands)
        base = f"http://127.0.0.1:{port}/account/payouts/"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(
                    queue_url=base + "?sub=payouts_choice",
                    demands_url=base + "?sub=demands",
                    metrics_path=str(Path(tmp) / "metrics.jsonl"),
                    debug_capture_attempts=False,
                    target_queue_size=5,
                    demands_sync_s=5,
                    take_poll_min_ms=50,
                    take_poll_max_ms=80,
                )
                ws = state.ensure(1, "Кабинет 1")
                ws.running, ws.mode = True, "take"
                ws.taken_order_ids, ws.demands_ids = set(), set()
                ws.taken_count = 0
                texts, notify, notify_file = self._collector()

                self._run_take_loop(cfg, ws, notify, notify_file, 0.6)

                joined = "\n".join(texts)
                self.assertIn("в работе уже 3/5", joined)
                self.assertIn("#9001, #9002, #9003", joined)
                self.assertIn("Возьму ещё 2", joined)
                # память пуста, но 3 слота заняты площадкой: берём ровно дефицит
                self.assertIn("9001", ws.demands_ids)
                self.assertEqual(ws.taken_order_ids, {"5001"})
                self.assertEqual(ws.taken_count, 1)
                self.assertEqual(len(posted), 1, posted)
                self.assertIn("payout_id=5001", posted[0])
        finally:
            server.shutdown()
            server.server_close()

    def test_restart_with_platform_queue_already_full_takes_nothing(self):
        """Память пуста (процесс перезапустили), но площадка уже держит 3/3 — брать нечего."""
        queue = _page_html(_row_html("5001", "Ждет провайдера"))
        demands = _page_html(
            *[_row_html(f"900{i}", "В работе у провайдера", with_form=False) for i in range(1, 4)]
        )
        server, posted, port = _start_payout_server(queue, demands)
        base = f"http://127.0.0.1:{port}/account/payouts/"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(
                    queue_url=base + "?sub=payouts_choice",
                    demands_url=base + "?sub=demands",
                    metrics_path=str(Path(tmp) / "metrics.jsonl"),
                    debug_capture_attempts=False,
                    target_queue_size=3,
                    demands_sync_s=5,
                    take_poll_min_ms=50,
                    take_poll_max_ms=80,
                )
                ws = state.ensure(1, "Кабинет 1")
                ws.running, ws.mode = True, "take"
                ws.taken_order_ids, ws.demands_ids = set(), set()
                texts, notify, notify_file = self._collector()

                self._run_take_loop(cfg, ws, notify, notify_file, 0.6)

                joined = "\n".join(texts)
                self.assertIn("в работе уже 3/3", joined)
                self.assertIn("Возьму ещё 0", joined)
                self.assertEqual(posted, [], "заявку брать нельзя: все слоты заняты площадкой")
                self.assertEqual(ws.taken_order_ids, set())
                self.assertEqual(ws.demands_ids, {"9001", "9002", "9003"})
                self.assertIn("очередь полная 3/3", ws.last_status)
        finally:
            server.shutdown()
            server.server_close()

    def test_autoissued_order_occupies_slot_and_is_reported(self):
        """Заявка, выданная площадкой без нашего «Забрать выплату», тоже занимает слот."""
        queue = _page_html(
            _row_html("6001", "Ждет провайдера"),
            _row_html("6002", "Ждет провайдера"),
        )
        page = {
            "demands": _page_html(
                _row_html("6001", "Ждет провайдера", with_form=False),
                _row_html("6002", "Ждет провайдера", with_form=False),
            )
        }
        server, posted, port = _start_payout_server(queue, lambda: page["demands"])
        base = f"http://127.0.0.1:{port}/account/payouts/"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(
                    queue_url=base + "?sub=payouts_choice",
                    demands_url=base + "?sub=demands",
                    metrics_path=str(Path(tmp) / "metrics.jsonl"),
                    debug_capture_attempts=False,
                    target_queue_size=1,
                    demands_sync_s=1,
                    take_poll_min_ms=50,
                    take_poll_max_ms=80,
                )
                ws = state.ensure(1, "Кабинет 1")
                ws.running, ws.mode = True, "take"
                ws.taken_order_ids, ws.demands_ids = set(), set()
                ws.taken_count = 0
                texts, notify, notify_file = self._collector()

                async def autoissue():
                    # площадка сама выдала 7777: в памяти бота её нет, но слот занят
                    page["demands"] = _page_html(
                        _row_html("6001", "В работе у провайдера", with_form=False),
                        _row_html("7777", "В работе у провайдера", with_form=False),
                    )

                self._run_take_loop(cfg, ws, notify, notify_file, 0.5, mid=autoissue, mid_wait=1.8)

                self.assertEqual(len(posted), 1, posted)
                self.assertIn("payout_id=6001", posted[0])
                self.assertEqual(ws.taken_order_ids, {"6001"})
                self.assertEqual(ws.demands_ids | ws.taken_order_ids, {"6001", "7777"})
                self.assertEqual(ws.slots_taken(), 2)
                self.assertIn("📦 Автовыдача: #7777", "\n".join(texts))
        finally:
            server.shutdown()
            server.server_close()

    def test_completed_order_frees_slot_and_taking_resumes(self):
        """Заявка доработала (статус уже не «в работе») — слот свободен, берём следующую."""
        queue = _page_html(_row_html("7001", "Ждет провайдера"))
        done = {"v": False}

        def demands_page() -> str:
            """Страница demands, зависящая от того, что бот уже взял (как на реальном сайте)."""
            in_work = set(ws.taken_order_ids)
            if done["v"]:
                in_work.discard("8001")  # заявка доработала — её слот освободился
            else:
                in_work.add("8001")      # заявка была в работе ещё до старта теста
            rows = [
                _row_html(oid, "В работе у провайдера", with_form=False)
                for oid in sorted(in_work)
            ]
            if "7001" not in in_work:
                rows.append(_row_html("7001", "Ждет провайдера", with_form=False))
            return _page_html(*rows)

        server, posted, port = _start_payout_server(queue, demands_page)
        base = f"http://127.0.0.1:{port}/account/payouts/"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(
                    queue_url=base + "?sub=payouts_choice",
                    demands_url=base + "?sub=demands",
                    metrics_path=str(Path(tmp) / "metrics.jsonl"),
                    debug_capture_attempts=False,
                    target_queue_size=1,
                    demands_sync_s=1,
                    take_poll_min_ms=50,
                    take_poll_max_ms=80,
                )
                ws = state.ensure(1, "Кабинет 1")
                ws.running, ws.mode = True, "take"
                # 8001 взяли мы, и она всё ещё занимала слот целевой очереди
                ws.taken_order_ids, ws.demands_ids = {"8001"}, set()
                ws.taken_count = 0
                texts, notify, notify_file = self._collector()

                async def finish():
                    done["v"] = True  # заявка 8001 доработала — слот освободился

                self._run_take_loop(cfg, ws, notify, notify_file, 0.4, mid=finish, mid_wait=1.8)

                joined = "\n".join(texts)
                self.assertIn("Заявка #8001", joined)
                self.assertIn("слот свободен", joined)
                self.assertNotIn("8001", ws.taken_order_ids)
                # слот освободился — бот добрал заявку из очереди
                self.assertEqual(ws.taken_order_ids, {"7001"})
                self.assertEqual(ws.taken_count, 1)
                self.assertEqual(len(posted), 1, posted)
                self.assertIn("payout_id=7001", posted[0])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
