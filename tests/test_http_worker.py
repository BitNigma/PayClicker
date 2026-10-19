"""Офлайн-тесты парсера и утилит http_worker на реальных HTML-дампах из ./data.

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from app.config import Account, Config
from app.http_worker import (
    STATUS_IN_WORK_MARKER,
    SITE_HOST,
    MAX_BLOCK_REAUTH,
    BlockedError,
    Metrics,
    PayoutClient,
    TAKE_DUMP_BODY_CHARS,
    TAKE_REPLY_GONE,
    TAKE_REPLY_STILL_AVAILABLE,
    TAKE_REPLY_TAKEN,
    TAKE_REPLY_UNKNOWN,
    _classify,
    _classify_take_response,
    _do_take,
    _dump_take_request,
    _effective_pollers,
    _fire_predicted_window,
    _observe_queue,
    _poll_loop,
    _poll_wait,
    _recover_from_block,
    ProxyPool,
    load_proxies,
    mask_proxy,
    _reset_stored_session,
    _safe_text,
    in_work_ids,
    load_cookies,
    parse_rows,
    predict_loop,
    take_loop,
)
from app.state import WorkerState, state

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

    def test_blocked_cloudflare_challenge(self):
        # Cloudflare отдаёт страницу-заглушку «Just a moment…» → это блок, а не ok.
        html = "<title>Just a moment...</title><div id='cf-chl-widget'></div>"
        self.assertEqual(_classify(html, "https://spark-trading.cash/account/payouts/"), "blocked")

    def test_normal_payouts_page_is_ok(self):
        html = "<table class='payout-table'><tr><td class='payout-status'># 1</td></tr></table>"
        self.assertEqual(_classify(html, "https://spark-trading.cash/account/payouts/"), "ok")


class BlockedResponseTest(unittest.TestCase):
    """_check должен распознавать 403/антибот, а не молча считать страницу валидной."""

    def _check(self, response):
        PayoutClient(make_config(), {})._check(response)

    def _resp(self, status, text):
        return httpx.Response(
            status,
            request=httpx.Request("GET", "https://spark-trading.cash/account/payouts/"),
            text=text,
        )

    def test_http_403_is_blocked(self):
        with self.assertRaises(BlockedError):
            self._check(self._resp(403, "<html>blocked</html>"))

    def test_cloudflare_challenge_body_is_blocked(self):
        with self.assertRaises(BlockedError):
            self._check(self._resp(200, "<title>Just a moment...</title>"))

    def test_normal_page_passes(self):
        # Не должно бросать исключение.
        self._check(self._resp(200, "<table class='payout-table'></table>"))


class ClassifyTakeResponseTest(unittest.TestCase):
    """Разбор ТЕЛА ответа на POST take_payout: отличить «взяли» от «нет заявки».

    Это диагностика к уведомлению; окончательное решение о взятии даёт сверка с
    demands (_confirm_taken), но понять ответ сервера мы обязаны.
    """

    def _cfg(self, **kw):
        return make_config(**kw)

    def test_gone_by_marker(self):
        cfg = self._cfg(take_gone_markers=("заявка не найдена", "уже взята"))
        html = "<html><body><div>Заявка не найдена</div></body></html>"
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_GONE)

    def test_taken_by_marker(self):
        cfg = self._cfg(take_success_markers=("успешно взята",))
        html = "<html><body><div>Заявка успешно взята</div></body></html>"
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_TAKEN)

    def test_gone_marker_has_priority(self):
        cfg = self._cfg(take_success_markers=("взята",), take_gone_markers=("уже взята",))
        html = "<html><body>Заявка уже взята</body></html>"
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_GONE)

    def test_taken_when_row_is_in_work(self):
        # В ответе осталась строка этого id со статусом «в работе у провайдера».
        cfg = self._cfg()
        html = _page_html(_row_html("6452828", "В работе у провайдера"))
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_TAKEN)

    def test_still_available_when_take_form_present(self):
        # Заявка всё ещё предлагается к взятию → наш POST не сработал.
        cfg = self._cfg()
        html = _page_html(_row_html("6452828", "Ждет провайдера"))
        self.assertEqual(
            _classify_take_response(html, "6452828", cfg), TAKE_REPLY_STILL_AVAILABLE
        )

    def test_gone_when_our_row_absent_from_queue_page(self):
        # Страница очереди есть, но НАШЕЙ строки нет → заявки больше нет (взяли/нет).
        cfg = self._cfg()
        html = _page_html(_row_html("9999999", "Ждет провайдера"))
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_GONE)

    def test_unknown_when_page_is_not_queue(self):
        # Ответ вообще не похож на страницу очереди (например, дашборд).
        cfg = self._cfg()
        html = "<html><body><div class='user-account-page'>личный кабинет</div></body></html>"
        self.assertEqual(_classify_take_response(html, "6452828", cfg), TAKE_REPLY_UNKNOWN)

    def test_only_matches_requested_id(self):
        cfg = self._cfg()
        html = _page_html(
            _row_html("6452828", "Ждет провайдера"),
            _row_html("6452829", "В работе у провайдера"),
        )
        # Форма осталась именно у нашей заявки — значит её мы не взяли.
        self.assertEqual(
            _classify_take_response(html, "6452828", cfg), TAKE_REPLY_STILL_AVAILABLE
        )
        # А у 6452829 статус «в работе» → считаем взятой.
        self.assertEqual(_classify_take_response(html, "6452829", cfg), TAKE_REPLY_TAKEN)


class DumpTakeRequestTest(unittest.TestCase):
    """Дамп POST-запроса должен содержать и ТЕЛО ответа — чтобы вручную видеть
    разницу между «взяли» и «такой заявки нет»."""

    def _dump(self, body: str, order_id: str = "6452828", server_reply=None, predicted=False):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(log_path=str(Path(tmp) / "actions.log"))
            response = httpx.Response(
                200,
                request=httpx.Request("POST", cfg.queue_url),
                text=body,
                headers={"content-type": "text/html; charset=utf-8"},
            )
            fp = _dump_take_request(
                cfg,
                order_id,
                {"csrf_token": "tok", "act": "take_payout", "payout_id": order_id, "access_key": ""},
                response,
                server_reply,
                predicted,
            )
            return json.loads(fp.read_text(encoding="utf-8")), fp.name

    def test_saves_response_body_and_order_id(self):
        payload, name = self._dump("<html>OK заявка взята</html>", server_reply=TAKE_REPLY_TAKEN)
        self.assertIn("6452828", name)
        self.assertEqual(payload["response"]["status"], 200)
        self.assertIn("OK заявка взята", payload["response"]["text_head"])
        self.assertIn("payout_id=6452828", payload["request"]["post_data"])
        # Вердикт разбора ответа тоже попадает в дамп.
        self.assertEqual(payload["response"]["server_reply"], TAKE_REPLY_TAKEN)

    def test_truncates_long_body(self):
        payload, _ = self._dump("X" * (TAKE_DUMP_BODY_CHARS + 5000))
        self.assertEqual(len(payload["response"]["text_head"]), TAKE_DUMP_BODY_CHARS)

    def test_handles_non_text_response(self):
        # Тело, которое нельзя декодировать как текст, не должно ронять дамп.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(log_path=str(Path(tmp) / "actions.log"))
            response = httpx.Response(
                200,
                request=httpx.Request("POST", cfg.queue_url),
                content=b"\xff\xfe\x00\x01",
            )
            fp = _dump_take_request(
                cfg, "1", {"csrf_token": "t", "act": "take_payout", "payout_id": "1", "access_key": ""}, response
            )
            self.assertTrue(fp.exists())

    def test_safe_text_returns_body_for_normal_response(self):
        resp = httpx.Response(
            200, request=httpx.Request("POST", "https://x/"), text="<html>ок</html>"
        )
        self.assertEqual(_safe_text(resp), "<html>ок</html>")

    def test_safe_text_falls_back_when_text_raises(self):
        # Если .text бросает (битая кодировка), _safe_text декодирует content грубо.
        class Boom:
            @property
            def text(self):
                raise UnicodeDecodeError("ascii", b"x", 0, 1, "boom")

            content = "привет".encode("utf-8")

        self.assertEqual(_safe_text(Boom()), "привет")

    def test_predicted_dump_has_suffix_and_flag(self):
        payload, name = self._dump("<html>x</html>", predicted=True)
        self.assertTrue(name.endswith("_pred.json"), name)
        self.assertTrue(payload["predicted"])

    def test_normal_dump_has_no_predicted_flag(self):
        payload, name = self._dump("<html>x</html>", predicted=False)
        self.assertFalse(name.endswith("_pred.json"))
        self.assertFalse(payload["predicted"])


class PredictedTakeMetricsTest(unittest.TestCase):
    """Предсказательная отправка должна быть видна в metrics.jsonl (флаг predicted)."""

    def test_take_metric_marks_predicted(self):
        ws = WorkerState(index=1, name="К")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(metrics_path=str(Path(tmp) / "m.jsonl"))
            metrics = Metrics(cfg)
            client = mock.Mock()

            async def take_payout(order_id, csrf_token, access_key=""):
                return httpx.Response(
                    200, request=httpx.Request("POST", cfg.queue_url), text="<html></html>"
                )

            async def fetch_demands():
                return _page_html()  # пустая таблица → не подтвердится

            client.take_payout = take_payout
            client.fetch_demands = fetch_demands

            async def notify(_text):
                pass

            row = {"id": "6467168", "csrf_token": "tok", "access_key": ""}
            confirmed = asyncio.run(
                _do_take(
                    client, cfg, cfg.accounts[0], ws, notify, metrics, row, {},
                    time.perf_counter(), set(), predicted=True,
                )
            )
            self.assertFalse(confirmed)
            lines = [
                json.loads(line)
                for line in Path(cfg.metrics_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            take = next(line for line in lines if line.get("event") == "take")
            self.assertTrue(take["predicted"])
            result = next(line for line in lines if line.get("event") == "take_result")
            self.assertTrue(result["predicted"])


class TakeRetryTest(unittest.TestCase):
    """Повтор POST take_payout при НЕОДНОЗНАЧНОМ ответе сервера.

    Если ответ «заявка ещё в очереди» (still_available) или не распознан (unknown)
    и взятие не подтвердилось — делаем ограниченное число повторов. taken/gone не
    повторяем, предсказанные id не повторяем (иначе POST-шторм).
    """

    def _run(self, *, post_html, demands_html, predicted=False,
             retry_count=2, retry_delay_ms=0):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                take_retry_count=retry_count,
                take_retry_delay_ms=retry_delay_ms,
                metrics_path=str(Path(tmp) / "m.jsonl"),
                debug_capture_attempts=False,
            )
            ws = WorkerState(index=1, name="К")
            metrics = Metrics(cfg)
            posts = []

            async def take_payout(order_id, csrf_token, access_key=""):
                posts.append(order_id)
                return httpx.Response(
                    200, request=httpx.Request("POST", cfg.queue_url), text=post_html
                )

            async def fetch_demands():
                return demands_html

            client = mock.Mock()
            client.take_payout = take_payout
            client.fetch_demands = fetch_demands

            async def notify(_text):
                pass

            row = {"id": "7001", "csrf_token": "tok", "access_key": ""}
            confirmed = asyncio.run(
                _do_take(
                    client, cfg, cfg.accounts[0], ws, notify, metrics, row, {},
                    time.perf_counter(), set(), predicted=predicted,
                )
            )
            lines = [
                json.loads(line)
                for line in Path(cfg.metrics_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return confirmed, posts, lines

    def test_retries_on_still_available_until_limit(self):
        still = _page_html(_row_html("7001", "Ждет провайдера"))
        confirmed, posts, lines = self._run(
            post_html=still, demands_html=_page_html(), retry_count=2
        )
        self.assertFalse(confirmed)
        self.assertEqual(posts, ["7001", "7001", "7001"])   # 1 попытка + 2 повтора
        takes = [r for r in lines if r.get("event") == "take"]
        self.assertEqual([r["attempt"] for r in takes], [1, 2, 3])

    def test_no_retry_when_order_gone(self):
        gone = _page_html()                                  # нашей строки нет
        confirmed, posts, _ = self._run(
            post_html=gone, demands_html=_page_html(), retry_count=2
        )
        self.assertFalse(confirmed)
        self.assertEqual(posts, ["7001"])                   # «уже забрали» — не повторяем

    def test_no_retry_for_predicted(self):
        still = _page_html(_row_html("7001", "Ждет провайдера"))
        _, posts, _ = self._run(
            post_html=still, demands_html=_page_html(), predicted=True, retry_count=2
        )
        self.assertEqual(posts, ["7001"])                   # предсказанные не повторяем

    def test_no_retry_when_confirmed(self):
        still = _page_html(_row_html("7001", "Ждет провайдера"))
        demands = _page_html(_row_html("7001", "В работе у провайдера", with_form=False))
        confirmed, posts, _ = self._run(
            post_html=still, demands_html=demands, retry_count=2
        )
        self.assertTrue(confirmed)
        self.assertEqual(posts, ["7001"])                   # взяли с первой попытки

    def test_retry_disabled_by_zero(self):
        still = _page_html(_row_html("7001", "Ждет провайдера"))
        _, posts, _ = self._run(
            post_html=still, demands_html=_page_html(), retry_count=0
        )
        self.assertEqual(posts, ["7001"])                   # TAKE_RETRY_COUNT=0 — без повторов


class ResetStoredSessionTest(unittest.TestCase):
    """Сброс сохранённых cookies кабинета (файла auth.json)."""

    def test_removes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text("{}", encoding="utf-8")
            account = make_config(storage_state_path=str(p)).accounts[0]
            self.assertTrue(_reset_stored_session(account))
            self.assertFalse(p.exists())

    def test_missing_file_returns_false(self):
        account = make_config(storage_state_path="/nonexistent/auth.json").accounts[0]
        self.assertFalse(_reset_stored_session(account))


class RecoverFromBlockTest(unittest.TestCase):
    """Авто-переавторизация после блокировки 403/антибот."""

    def test_resets_session_and_reauths(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "auth.json"
            p.write_text("{}", encoding="utf-8")
            account = make_config(storage_state_path=str(p)).accounts[0]
            ws = WorkerState(index=1, name="Кабинет 1")
            sent = []

            async def notify(text):
                sent.append(text)

            async def fake_ensure(cfg, account, ws, notify, notify_file=None,
                                  force_reauth=False, proxy=None):
                self.assertTrue(force_reauth)  # переавторизация идёт с нуля
                return {"PHPSESSID": "x"}

            with mock.patch("app.http_worker.ensure_session", fake_ensure):
                ok = asyncio.run(_recover_from_block(make_config(), account, ws, notify, notify))
            self.assertTrue(ok)
            self.assertEqual(ws.reauth_attempts, 1)
            self.assertFalse(p.exists())  # cookies сброшены
            self.assertTrue(any("Сбрасываю сохранённые cookies" in m for m in sent))

    def test_stops_after_max_attempts(self):
        account = make_config().accounts[0]
        ws = WorkerState(index=1, name="К")
        ws.reauth_attempts = MAX_BLOCK_REAUTH
        sent = []

        async def notify(text):
            sent.append(text)

        ok = asyncio.run(_recover_from_block(make_config(), account, ws, notify, notify))
        self.assertFalse(ok)
        self.assertTrue(any("не помогла" in m for m in sent))


class ProxySupportTest(unittest.TestCase):
    """Прокси: список из env/файла, ротация IP при блокировке, маскирование."""

    def test_load_proxies_from_env_and_file_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "proxies.txt"
            pf.write_text(
                "# комментарий\nhttp://p2:8080\nhttp://p1:8080\nhttp://p2:8080\n",
                encoding="utf-8",
            )
            cfg = make_config(
                proxy_urls=["http://p1:8080", "http://p3:8080"],
                proxy_file=str(pf),
            )
            self.assertEqual(
                load_proxies(cfg),
                ["http://p1:8080", "http://p3:8080", "http://p2:8080"],
            )

    def test_proxy_pool_rotates_and_reloads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "proxies.txt"
            pf.write_text("http://a:1\nhttp://b:2\n", encoding="utf-8")
            cfg = make_config(proxy_urls=[], proxy_file=str(pf))
            pool = ProxyPool(cfg)
            self.assertEqual(pool.count, 2)
            self.assertEqual(pool.current(), "http://a:1")
            # файл дополнили на лету — rotate перечитывает его
            pf.write_text("http://a:1\nhttp://b:2\nhttp://c:3\n", encoding="utf-8")
            self.assertEqual(pool.rotate(), "http://b:2")
            self.assertEqual(pool.rotate(), "http://c:3")
            self.assertEqual(pool.rotate(), "http://a:1")

    def test_mask_proxy_hides_credentials(self):
        self.assertEqual(mask_proxy("http://user:pass@1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(mask_proxy(None), "direct")

    def test_payout_client_accepts_proxy(self):
        cfg = make_config()
        client = PayoutClient(cfg, {}, proxy="http://127.0.0.1:9")
        try:
            self.assertEqual(client.proxy, "http://127.0.0.1:9")
        finally:
            asyncio.run(client.aclose())

    def test_recover_from_block_rotates_proxy_before_reauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "proxies.txt"
            pf.write_text("http://a:1\nhttp://b:2\n", encoding="utf-8")
            cfg = make_config(proxy_file=str(pf), proxy_rotate_on_block=True)
            account = cfg.accounts[0]
            ws = WorkerState(index=1, name="К")
            sent = []

            async def notify(text):
                sent.append(text)

            pool = ProxyPool(cfg)
            ok = asyncio.run(_recover_from_block(cfg, account, ws, notify, notify, pool))
            self.assertTrue(ok)
            self.assertEqual(ws.proxy_rotations, 1)
            self.assertEqual(ws.reauth_attempts, 0)   # cookies НЕ сбрасывали
            self.assertTrue(any("меняю прокси" in m for m in sent))


class PredictiveTakeTest(unittest.TestCase):
    """Превентивное взятие: шагаем вперёд окнами по count id от пойманного id."""

    def _cfg(self, **kw):
        kw.setdefault("predictive_take", True)
        kw.setdefault("predictive_take_count", 3)
        return make_config(**kw)

    @staticmethod
    def _fake_do_take(calls, on_call=None):
        async def fake(client, cfg, account, ws, notify, metrics, row, timing,
                       t_recv, inflight, predicted=False):
            calls.append((row["id"], predicted))
            if on_call is not None:
                return on_call(row)
            return True
        return fake

    def test_observe_queue_sets_anchor_and_cursor(self):
        ws = WorkerState(index=1, name="К")
        rows = [
            {"id": "6442130", "csrf_token": "tok", "access_key": ""},
            {"id": "6442137", "csrf_token": "tok", "access_key": ""},
        ]
        _observe_queue(ws, rows)
        self.assertEqual(ws.observed_anchor, 6442137)
        self.assertEqual(ws.last_order_id, 6442137)
        self.assertEqual(ws.last_csrf_token, "tok")

    def test_observe_queue_resets_predictive_pool(self):
        """Поймав реальную заявку, отсчёт пула сбрасывается от неё — а не от
        «убежавшего» вперёд предсказанного курсора."""
        ws = WorkerState(index=1, name="К")
        ws.observed_anchor = 6450000
        ws.last_order_id = 6450500            # курсор убежал вперёд
        ws.predictive_sent = {"6450501", "6450502"}
        _observe_queue(ws, [{"id": "6450020", "csrf_token": "tok"}])
        self.assertEqual(ws.observed_anchor, 6450020)
        self.assertEqual(ws.last_order_id, 6450020)  # пул сброшен на реальный id
        self.assertEqual(ws.predictive_sent, set())

    def test_fire_window_advances_cursor(self):
        ws = WorkerState(index=1, name="К")
        ws.last_order_id = 6442137
        ws.last_csrf_token = "tok"
        cfg = self._cfg()
        account = cfg.accounts[0]
        calls = []

        async def run():
            with mock.patch("app.http_worker._do_take", self._fake_do_take(calls)):
                tasks = _fire_predicted_window(
                    None, cfg, account, ws, None, None, set(), set()
                )
                await asyncio.gather(*tasks)
        asyncio.run(run())
        self.assertEqual([c[0] for c in calls], ["6442138", "6442139", "6442140"])
        self.assertTrue(all(c[1] for c in calls))  # все — предсказанные
        self.assertEqual(ws.last_order_id, 6442140)  # курсор = наибольший id окна

    def test_fire_window_skips_known_ids(self):
        ws = WorkerState(index=1, name="К")
        ws.last_order_id = 6442137
        ws.last_csrf_token = "tok"
        ws.demands_ids = {"6442138"}
        cfg = self._cfg()
        account = cfg.accounts[0]
        calls = []

        async def run():
            with mock.patch("app.http_worker._do_take", self._fake_do_take(calls)):
                tasks = _fire_predicted_window(
                    None, cfg, account, ws, None, None, set(), set()
                )
                await asyncio.gather(*tasks)
        asyncio.run(run())
        self.assertEqual([c[0] for c in calls], ["6442139", "6442140"])

    def test_predict_loop_notifies_once_per_anchor(self):
        """Уведомление о превентивных POST шлётся ОДИН раз на якорь, а не каждые
        PREDICTIVE_NOTIFY_MIN_S с неизменным текстом. Была жалоба, что сообщение
        «🔮 Превентивно: N POST take_payout (окно от #…)» дублировалось в Telegram:
        текст строился из ws.observed_anchor, который не меняется, пока нет новой
        реальной заявки."""
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6442137
        ws.observed_anchor = 6442137       # якорь не меняется → сообщение одно
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_gap_ms=0, predictive_take_lookahead=0, debug=True)
        account = cfg.accounts[0]
        calls = []
        messages = []

        async def notify(text):
            messages.append(text)

        def stop_after_windows(_row):
            if len(calls) >= 9:            # три окна по 3 — останавливаем фон
                ws.running = False
            return False

        async def run():
            # Троттлинг отключаем (он и так не даёт спамить быстрее 3 с) — проверяем
            # именно дедуп по якорю: без него было бы по сообщению на КАЖДОЕ окно.
            with mock.patch("app.http_worker.PREDICTIVE_NOTIFY_MIN_S", 0.0), \
                    mock.patch("app.http_worker._do_take",
                               self._fake_do_take(calls, on_call=stop_after_windows)):
                await predict_loop(None, cfg, account, ws, notify, None, set(), set())
        asyncio.run(run())
        self.assertGreaterEqual(len(calls), 9, calls)
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("окно #6442138…", messages[0])

    def test_failed_window_advances_and_fires_next(self):
        """Если окно целиком неудачное — берём наибольший id окна и шлём следующее."""
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6442137
        ws.observed_anchor = 6442137
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_gap_ms=0, predictive_take_lookahead=0)
        account = cfg.accounts[0]
        calls = []

        def stop_after_two_windows(_row):
            if len(calls) >= 6:  # два окна по 3 — останавливаем фон
                ws.running = False
            return False  # все запросы неудачные

        async def run():
            with mock.patch("app.http_worker._do_take",
                            self._fake_do_take(calls, on_call=stop_after_two_windows)):
                await predict_loop(None, cfg, account, ws, None, None, set(), set())
        asyncio.run(run())
        self.assertEqual(
            [c[0] for c in calls],
            ["6442138", "6442139", "6442140", "6442141", "6442142", "6442143"],
        )

    def test_successful_window_resets_rounds(self):
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6442137
        ws.observed_anchor = 6442137
        ws.last_csrf_token = "tok"
        ws.predictive_rounds = 3
        cfg = self._cfg(predictive_take_gap_ms=0, predictive_take_lookahead=0)
        account = cfg.accounts[0]
        calls = []

        def stop_after_first(_row):
            ws.running = False
            return True  # заявку поймали

        async def run():
            with mock.patch("app.http_worker._do_take",
                            self._fake_do_take(calls, on_call=stop_after_first)):
                await predict_loop(None, cfg, account, ws, None, None, set(), set())
        asyncio.run(run())
        self.assertEqual(ws.predictive_rounds, 0)  # успех сбрасывает счётчик

    def test_slots_taken_excludes_speculative(self):
        """Спекулятивные (предсказанные) отправки не считаются занятыми слотами."""
        ws = WorkerState(index=1, name="К")
        ws.demands_ids = {"100"}
        inflight = {"101", "102"}  # 101 — реальная отправка, 102 — предсказание
        self.assertEqual(ws.slots_taken(inflight), 3)
        self.assertEqual(ws.slots_taken(inflight, {"102"}), 2)

    def test_predict_loop_stops_when_queue_full(self):
        """Когда реальная очередь заполнена — предсказатель не бежит вперёд."""
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6450000
        ws.observed_anchor = 6450000
        ws.last_csrf_token = "tok"
        ws.demands_ids = {str(6449000 + i) for i in range(10)}  # цель=10, очередь полна
        cfg = self._cfg(predictive_take_gap_ms=0, predictive_take_lookahead=0)
        calls = []

        async def run():
            with mock.patch("app.http_worker._do_take", self._fake_do_take(calls)):
                task = asyncio.create_task(
                    predict_loop(None, cfg, cfg.accounts[0], ws, None, None, set(), set())
                )
                await asyncio.sleep(0.05)
                ws.running = False
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
        self.assertEqual(calls, [])  # нет свободного слота — ничего не предсказываем

    def test_predict_loop_reanchors_when_reality_overtakes(self):
        """Если реальность обогнала курсор — догоняем её и предсказываем от неё."""
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.observed_anchor = 6450100  # реальность ушла вперёд
        ws.last_order_id = 6450000    # курсор отстал
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_count=3, predictive_take_gap_ms=0,
                        predictive_take_lookahead=0)
        calls = []

        def stop_after_first_window(_row):
            if len(calls) >= 3:
                ws.running = False
            return False

        async def run():
            with mock.patch("app.http_worker._do_take",
                            self._fake_do_take(calls, on_call=stop_after_first_window)):
                await predict_loop(None, cfg, cfg.accounts[0], ws, None, None, set(), set())
        asyncio.run(run())
        self.assertEqual([c[0] for c in calls], ["6450101", "6450102", "6450103"])

    def test_predicted_inflight_cleared_after_window(self):
        """Спекулятивные отправки не «залипают»: после окна набор пуст."""
        ws = WorkerState(index=1, name="К")
        ws.last_order_id = 6442137
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_count=3)
        calls = []

        async def run():
            with mock.patch("app.http_worker._do_take", self._fake_do_take(calls)):
                tasks = _fire_predicted_window(
                    None, cfg, cfg.accounts[0], ws, None, None, set(), set()
                )
                await asyncio.gather(*tasks)
        asyncio.run(run())
        self.assertEqual(ws.predicted_inflight, set())

    def test_predict_loop_does_not_run_past_lookahead(self):
        """Курсор не уходит дальше `lookahead` от якоря, а ДОЙДЯ до предела
        предсказатель ПОВТОРНО обстреливает тот же диапазон (retry), а не встаёт
        ждать. Иначе заявку, чей id уже пробовали (когда её ещё не было), мы бы не
        поймали — это и было «пропускаем все заявки».

        count=3, lookahead=6 -> проход 1..9, затем снова с 1 (повторы).
        """
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6450000
        ws.observed_anchor = 6450000
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_count=3, predictive_take_gap_ms=10,
                        predictive_take_lookahead=6)
        calls = []

        async def fake(client, cfg, account, ws, notify, metrics, row, timing,
                       t_recv, inflight, predicted=False):
            # Как реальный _do_take: по завершении освобождаем id, иначе повторный
            # обстрел «залипнет» на уже отправленных id.
            calls.append((row["id"], predicted))
            inflight.discard(row["id"])
            return False

        async def run():
            with mock.patch("app.http_worker._do_take", fake):
                task = asyncio.create_task(
                    predict_loop(None, cfg, cfg.accounts[0], ws, None, None, set(), set())
                )
                await asyncio.sleep(0.3)  # время на проход фронта и повтор
                ws.running = False
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())

        ids = [int(c[0]) for c in calls]
        self.assertGreaterEqual(len(ids), 12, ids)
        # проход по окнам вперёд, строго по порядку, до предела lookahead
        self.assertEqual(ids[:9], [6450001, 6450002, 6450003,
                                   6450004, 6450005, 6450006,
                                   6450007, 6450008, 6450009])
        # на пределе — ПОВТОР с того же места, а не новые большие id
        self.assertEqual(ids[9:12], [6450001, 6450002, 6450003])
        self.assertLessEqual(max(ids), 6450009)
        self.assertEqual(ws.observed_anchor, 6450000)

    def test_predict_loop_resumes_after_new_observation(self):
        """(1) При статичной реальности предсказатель НЕ встаёт — он повторно
        обстреливает фронт. (2) Когда реальность уходит вперёд (observed_anchor
        растёт), курсор догоняет её и обстрел идёт от нового id."""
        ws = WorkerState(index=1, name="К")
        ws.running = True
        ws.mode = "take"
        ws.last_order_id = 6450000
        ws.observed_anchor = 6450000
        ws.last_csrf_token = "tok"
        cfg = self._cfg(predictive_take_count=3, predictive_take_gap_ms=10,
                        predictive_take_lookahead=3)
        calls = []

        async def wait_for(cond, timeout=1.0):
            waited = 0.0
            while not cond() and waited < timeout:
                await asyncio.sleep(0.01)
                waited += 0.01

        async def fake(client, cfg, account, ws, notify, metrics, row, timing,
                       t_recv, inflight, predicted=False):
            # Как реальный _do_take: по завершении освобождаем id для повторного обстрела.
            calls.append((row["id"], predicted))
            inflight.discard(row["id"])
            return False

        async def run():
            with mock.patch("app.http_worker._do_take", fake):
                task = asyncio.create_task(
                    predict_loop(None, cfg, cfg.accounts[0], ws, None, None, set(), set())
                )
                # фаза 1: фронт обстрелян
                await wait_for(lambda: len(calls) >= 6)
                n1 = len(calls)
                # фаза 2: реальность статична, но предсказатель продолжает (retry)
                await wait_for(lambda: len(calls) > n1)
                self.assertGreater(len(calls), n1)
                # фаза 3: реальность ушла вперёд — курсор догоняет её и обстрел от нового id
                ws.observed_anchor = 6450500
                await wait_for(lambda: any(int(c[0]) > 6450500 for c in calls))
                self.assertTrue(any(int(c[0]) > 6450500 for c in calls))
                ws.running = False
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())


class PredictivePollIntervalTest(unittest.TestCase):
    """При включённом предиктиве опрос очереди РЕЖЕ: заявки ловит predict_loop
    спекулятивными POST, а частые GET лишь ловят бан Cloudflare."""

    def test_poll_wait_slow_when_predictive_on(self):
        cfg = make_config(predictive_take=True, predictive_take_poll_ms=1000,
                          take_poll_min_ms=200, take_poll_max_ms=300)
        for _ in range(30):
            self.assertGreaterEqual(_poll_wait(cfg), 1.0)
            self.assertLessEqual(_poll_wait(cfg), 1.5)

    def test_poll_wait_normal_when_predictive_off(self):
        cfg = make_config(predictive_take=False, predictive_take_poll_ms=1000,
                          take_poll_min_ms=200, take_poll_max_ms=300)
        for _ in range(30):
            self.assertGreaterEqual(_poll_wait(cfg), 0.2)
            self.assertLessEqual(_poll_wait(cfg), 0.3)

    def test_poll_wait_zero_keeps_normal_even_with_predictive(self):
        cfg = make_config(predictive_take=True, predictive_take_poll_ms=0,
                          take_poll_min_ms=200, take_poll_max_ms=300)
        for _ in range(30):
            self.assertGreaterEqual(_poll_wait(cfg), 0.2)
            self.assertLessEqual(_poll_wait(cfg), 0.3)

    def test_default_is_active_hot_poll_even_with_predictive(self):
        """По умолчанию (PREDICTIVE_TAKE_POLL_MS не задан = 0) горячий GET-цикл
        АКТИВЕН и с предиктивом: пауза из TAKE_POLL_MIN/MAX_MS и все воронки.
        Предиктив только добавляет POST, а не заменяет опрос."""
        cfg = make_config(predictive_take=True,
                          take_poll_min_ms=200, take_poll_max_ms=300,
                          take_parallel_pollers=3)
        self.assertEqual(cfg.predictive_take_poll_ms, 0)
        for _ in range(20):
            self.assertGreaterEqual(_poll_wait(cfg), 0.2)
            self.assertLessEqual(_poll_wait(cfg), 0.3)
        self.assertEqual(_effective_pollers(cfg), 3)

    def test_effective_pollers_kept_when_interval_faster_than_normal(self):
        """PREDICTIVE_TAKE_POLL_MS меньше обычного интервала (напр. 1 мс =
        «максимально быстро») — воронки НЕ режем: иначе «ускорение» парадоксально
        уменьшало бы их число."""
        cfg = make_config(predictive_take=True, predictive_take_poll_ms=1,
                          take_poll_min_ms=150, take_poll_max_ms=200,
                          take_parallel_pollers=3)
        self.assertLess(_poll_wait(cfg), 0.01)   # ~1 мс
        self.assertEqual(_effective_pollers(cfg), 3)

    def test_effective_pollers_capped_to_one_with_predictive(self):
        """С предиктивом лишние воронки опроса — только GET-нагрузка: оставляем 1."""
        cfg = make_config(predictive_take=True, predictive_take_poll_ms=1000,
                          take_parallel_pollers=3)
        self.assertEqual(_effective_pollers(cfg), 1)

    def test_effective_pollers_kept_without_predictive(self):
        cfg = make_config(predictive_take=False, take_parallel_pollers=3)
        self.assertEqual(_effective_pollers(cfg), 3)

    def test_effective_pollers_kept_when_poll_ms_zero(self):
        cfg = make_config(predictive_take=True, predictive_take_poll_ms=0,
                          take_parallel_pollers=3)
        self.assertEqual(_effective_pollers(cfg), 3)

    def test_poll_loop_sleeps_poll_wait(self):
        """_poll_loop реально берёт паузу из _poll_wait (а не жёстко TAKE_POLL_*)."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                predictive_take=True, predictive_take_poll_ms=1000,
                metrics_path=str(Path(tmp) / "m.jsonl"),
            )
            ws = WorkerState(index=1, name="К")
            ws.running = True
            ws.mode = "take"
            ws.last_csrf_token = "tok"

            class FakeClient:
                async def fetch_queue(self):
                    return _page_html()      # пустая таблица → кандидатов нет

            async def notify(_text):
                pass

            sleeps = []

            async def fake_sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) >= 3:
                    ws.running = False

            async def run():
                with mock.patch("app.http_worker.asyncio.sleep", fake_sleep):
                    await _poll_loop(
                        FakeClient(), cfg, cfg.accounts[0], ws, notify, notify,
                        Metrics(cfg), set(), set(),
                    )
            asyncio.run(run())
            self.assertTrue(sleeps, sleeps)
            self.assertGreaterEqual(sleeps[0], 1.0)   # предиктив → редкий опрос


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

    def test_slots_taken_hidden_while_resync_pending(self):
        """Пока /reset_session не сверён с площадкой, воркер считает слоты пустыми
        и не берёт заявки на основании очищенного кэша."""
        ws = state.ensure(1, "Кабинет 1")
        ws.demands_ids = {"10", "11"}
        ws.taken_order_ids = {"11", "12"}
        ws.resync_required = True
        self.assertEqual(ws.slots_taken(), 0)

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

    def test_reset_session_resyncs_cache_from_platform(self):
        """/reset_session при работающем воркере: кэш очищен, но перед взятием новых
        заявок воркер пересчитывает очередь по площадке (и «мёртвые» id из памяти
        не «оживают» после сброса — берём только заявки, которых нет на demands)."""
        queue = _page_html(_row_html("7001", "Ждет провайдера"))
        # На площадке реально «в работе» только одна заявка, хотя в кэше бота их 10.
        demands = _page_html(
            _row_html("9001", "В работе у провайдера", with_form=False),
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
                    target_queue_size=1,
                    demands_sync_s=5,
                    take_poll_min_ms=50,
                    take_poll_max_ms=80,
                )
                ws = state.ensure(1, "Кабинет 1")
                ws.running, ws.mode = True, "take"
                # «Накопившийся кэш»: 9 мёртвых id + 1 реальная на площадке.
                ws.taken_order_ids = {str(1000 + i) for i in range(10)}
                ws.demands_ids = {str(1000 + i) for i in range(10)}
                ws.taken_count = 10
                # Команда /reset_session уже выполнилась: кэш очищен, флаг стоит.
                ws.reset_queue_cache()
                self.assertTrue(ws.resync_required)
                texts, notify, notify_file = self._collector()

                async def run():
                    import asyncio as _asyncio
                    from app.http_worker import PayoutClient as _PC, Metrics as _M

                    client = _PC(cfg, {})
                    try:
                        task = _asyncio.create_task(
                            take_loop(client, cfg, cfg.accounts[0], ws, notify, notify_file, _M(cfg))
                        )
                        await _asyncio.sleep(1.0)
                        ws.running = False
                        await _asyncio.wait_for(task, timeout=5)
                    finally:
                        await client.aclose()

                asyncio.run(run())
                joined = "\n".join(texts)
                # Ресинхронизация прошла, флаг снят, кэш = реальная картина площадки.
                self.assertFalse(ws.resync_required)
                self.assertEqual(ws.demands_ids, {"9001"})
                self.assertEqual(ws.taken_order_ids, set())
                self.assertIn("очередь пересчитана по площадке", joined)
                # Целевая очередь 1/1 — новая заявка из очереди НЕ берётся.
                self.assertEqual(len(posted), 0, posted)
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
