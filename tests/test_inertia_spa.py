"""Тесты SPA-площадки на Inertia (911pay): JSON-очередь, uuid и claim без тела.

Реальный захват (data/take_capture/take_unknown_*.json) показал:
  • очередь приходит JSON'ом props.payouts.data; id заявки — uuid, статус — код
    (unassigned/pending/wait_admin/returned_to_trader/success/canceled/expired);
  • взятие — POST /trader/payouts/{uuid}/claim БЕЗ тела;
  • csrf — заголовок X-XSRF-TOKEN, значение из cookie XSRF-TOKEN (URL-encoded).
Здесь проверяем, что парсер и клиент воспроизводят именно это.
"""
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.http_worker import PayoutClient, _confirm_taken, _dump_take_request, parse_rows
from tests.test_http_worker import make_config

UUID_FREE = "8db375f9-e7b7-4daa-bef0-36f665497828"
UUID_WORK = "6878c6b7-6fb0-4f9a-a735-32773e877130"
UUID_DONE = "01c165d9-8a93-4c99-8ced-87f85c81d2af"


def _inertia_payload(rows: list[dict]) -> dict:
    return {
        "component": "Trader/Payouts/PayoutList",
        "props": {"payouts": {"data": rows, "total": len(rows), "per_page": 10}},
        "url": "/trader/payouts/payout-list",
        "version": "v1",
    }


class InertiaParseTest(unittest.TestCase):
    def test_parses_json_response_with_uuid_and_status_label(self):
        payload = _inertia_payload(
            [
                {"id": 15658, "uuid": UUID_FREE, "status": "unassigned"},
                {"id": 1, "uuid": UUID_WORK, "status": "pending"},
                {"id": 2, "uuid": UUID_DONE, "status": "expired"},
            ]
        )
        rows = parse_rows(json.dumps(payload), make_config())
        self.assertEqual([r["id"] for r in rows], [UUID_FREE, UUID_WORK, UUID_DONE])
        self.assertEqual([r["status"] for r in rows], ["Не назначено", "В обработке", "Истекла"])

    def test_parses_html_data_page(self):
        """Обычный GET отдаёт HTML, состояние — в data-page="…json…" (SSR)."""
        payload = json.dumps(_inertia_payload([{"uuid": UUID_FREE, "status": "unassigned"}]))
        # Inertia экранирует кавычки как &quot; — так и приходит в HTML.
        escaped = payload.replace('"', "&quot;")
        html = f'<html><body><div id="app" data-page="{escaped}"></div></body></html>'
        rows = parse_rows(html, make_config())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], UUID_FREE)
        self.assertEqual(rows[0]["status"], "Не назначено")

    def test_unknown_status_code_is_kept(self):
        payload = _inertia_payload([{"uuid": UUID_FREE, "status": "brand_new_code"}])
        rows = parse_rows(json.dumps(payload), make_config())
        self.assertEqual(rows[0]["status"], "brand_new_code")

    def test_empty_payouts_gives_empty_list(self):
        rows = parse_rows(json.dumps(_inertia_payload([])), make_config())
        self.assertEqual(rows, [])

    def test_plain_html_still_parsed_by_old_parser(self):
        html = (
            "<table class='payout-table'><tbody><tr><td class='payout-status'>"
            "<b># 6408416</b><span class='badge'>Ждет провайдера</span></td></tr></tbody></table>"
        )
        rows = parse_rows(html, make_config())
        self.assertEqual(rows[0]["id"], "6408416")


def _start_claim_server():
    """Локальный «claim»-эндпоинт: записывает метод, путь, тело и заголовки."""
    captured: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b""
            captured.append(
                {
                    "path": self.path,
                    "body": body,
                    "xsrf": self.headers.get("X-XSRF-TOKEN"),
                    "xrw": self.headers.get("X-Requested-With"),
                    "ctype": self.headers.get("Content-Type"),
                }
            )
            payload = json.dumps({"success": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, captured


class SpaClaimRequestTest(unittest.TestCase):
    def _cfg(self, port: int, **overrides):
        base = dict(
            queue_url=f"http://127.0.0.1:{port}/trader/payouts/payout-list",
            demands_url=f"http://127.0.0.1:{port}/trader/payouts/payout-list?tab=common",
            take_api_url_template=f"http://127.0.0.1:{port}/trader/payouts/{{id}}/claim",
            take_payload_format="none",
            take_requires_csrf=False,
            take_csrf_header="X-XSRF-TOKEN",
            take_csrf_cookie="XSRF-TOKEN",
            take_id_field="",
            take_csrf_field="",
            take_access_key_field="",
            take_act_field="",
            take_act_value="",
            debug_capture_attempts=False,
        )
        base.update(overrides)
        return make_config(**base)

    def test_claim_is_post_without_body_and_xsrf_header_from_cookie(self):
        server, captured = _start_claim_server()
        port = server.server_address[1]
        cfg = self._cfg(port)
        # Cookie хранится URL-encoded (как в auth.json): в заголовок должно уйти
        # ДЕКОДИРОВАННОЕ значение, иначе Laravel отвечает 419 CSRF mismatch.
        client = PayoutClient(cfg, {"XSRF-TOKEN": "abc%3D%3D"})

        async def run():
            try:
                return await client.take_payout(UUID_FREE, None)
            finally:
                await client.aclose()

        try:
            response = asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req["path"], f"/trader/payouts/{UUID_FREE}/claim")
        self.assertEqual(req["body"], b"", "тело POST должно быть пустым")
        self.assertEqual(req["xsrf"], "abc==", "csrf-заголовок должен быть URL-декодирован")
        self.assertEqual(req["xrw"], "XMLHttpRequest")
        self.assertIsNone(req["ctype"], "content-type у POST без тела не нужен")

    def test_no_csrf_header_when_not_configured(self):
        server, captured = _start_claim_server()
        port = server.server_address[1]
        cfg = self._cfg(port, take_csrf_header="", take_csrf_cookie="")
        client = PayoutClient(cfg, {"XSRF-TOKEN": "abc"})

        async def run():
            try:
                return await client.take_payout(UUID_FREE, None)
            finally:
                await client.aclose()

        try:
            asyncio.run(run())
        finally:
            server.shutdown()
            server.server_close()

        self.assertIsNone(captured[0]["xsrf"])
        self.assertIsNone(captured[0]["xrw"])


class _FakeDemandsClient:
    def __init__(self, html: str):
        self.html = html

    async def fetch_demands(self) -> str:
        return self.html


class ConfirmStrictInWorkTest(unittest.TestCase):
    """CONFIRM_REQUIRES_IN_WORK: доказательство — только статус «в работе»."""

    def _cfg(self):
        return make_config(
            confirm_requires_in_work=True,
            status_in_work_markers=("в обработке", "ожидает подтверждения"),
            status_done_markers=("подтверждена", "отменена", "истекла"),
        )

    def _confirm(self, status_code: str) -> bool:
        html = json.dumps(_inertia_payload([{"uuid": UUID_FREE, "status": status_code}]))
        return asyncio.run(
            _confirm_taken(_FakeDemandsClient(html), self._cfg(), UUID_FREE, attempts=1, delay=0)
        )

    def test_pending_confirms(self):
        self.assertTrue(self._confirm("pending"))

    def test_wait_admin_confirms(self):
        self.assertTrue(self._confirm("wait_admin"))

    def test_expired_does_not_confirm(self):
        """422 «срок выплаты истёк» → в demands строка expired — это НЕ взятие."""
        self.assertFalse(self._confirm("expired"))

    def test_canceled_does_not_confirm(self):
        self.assertFalse(self._confirm("canceled"))

    def test_success_does_not_confirm(self):
        # Завершённую заявку нельзя записывать в «только что взяли»: за время
        # подтверждения статус не должен успеть пройти pending → success.
        self.assertFalse(self._confirm("success"))

    def test_unassigned_does_not_confirm(self):
        self.assertFalse(self._confirm("unassigned"))


def _start_inertia_site():
    """Локальный сайт-«911pay»: JSON-очередь, claim без тела и demands in-work."""
    state = {"claimed": False}
    captured: list[dict] = []

    def queue_payload():
        rows = [] if state["claimed"] else [{"id": 15658, "uuid": UUID_FREE, "status": "unassigned"}]
        return json.dumps(_inertia_payload(rows))

    def demands_payload():
        rows = [{"id": 15658, "uuid": UUID_FREE, "status": "pending"}] if state["claimed"] else []
        return json.dumps(_inertia_payload(rows))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, text: str):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(demands_payload() if "tab=in-work" in self.path else queue_payload())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            captured.append({"path": self.path, "body": self.rfile.read(length) if length else b""})
            state["claimed"] = True
            self._send(json.dumps({"success": True}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, captured


class TakeLoopInertiaTest(unittest.TestCase):
    """Сквозной такт: JSON-очередь → POST claim без тела → подтверждение in-work."""

    def test_take_loop_claims_uuid_without_body(self):
        from app.http_worker import Metrics, take_loop
        from app.state import state

        server, captured = _start_inertia_site()
        port = server.server_address[1]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                queue_url=f"http://127.0.0.1:{port}/trader/payouts/payout-list",
                demands_url=f"http://127.0.0.1:{port}/trader/payouts/payout-list?tab=in-work",
                take_api_url_template=f"http://127.0.0.1:{port}/trader/payouts/{{id}}/claim",
                take_payload_format="none",
                take_requires_csrf=False,
                take_retry_count=0,
                confirm_requires_in_work=True,
                status_in_work_markers=("в обработке",),
                status_done_markers=("отменена", "истекла"),
                target_queue_size=1,
                take_poll_min_ms=50,
                take_poll_max_ms=80,
                take_parallel_pollers=1,
                debug_capture_attempts=False,
                metrics_path=str(Path(tmp) / "metrics.jsonl"),
            )
            ws = state.ensure(1, "Кабинет 1")
            ws.running = True
            ws.mode = "take"
            ws.taken_order_ids = set()
            ws.demands_ids = set()
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
                    await asyncio.sleep(1.0)
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

        self.assertTrue(captured, "claim-запрос не отправлен")
        self.assertEqual(captured[0]["path"], f"/trader/payouts/{UUID_FREE}/claim")
        self.assertEqual(captured[0]["body"], b"", "тело claim должно быть пустым")
        self.assertIn(UUID_FREE, ws.taken_order_ids)


class ClassifySpaTakeReplyTest(unittest.TestCase):
    def test_expired_message_is_gone(self):
        """Реальный ответ 422: Laravel экранирует юникод в JSON — разбор по message."""
        from app.http_worker import TAKE_REPLY_GONE, _classify_take_response

        body = (
            '{"message":"\\u0421\\u0440\\u043e\\u043a \\u0432\\u044b\\u043f\\u043b'
            '\\u0430\\u0442\\u044b \\u0438\\u0441\\u0442\\u0451\\u043a \\u2014 '
            '\\u0432\\u043e\\u0437\\u044c\\u043c\\u0438\\u0442\\u0435 \\u0434\\u0440'
            '\\u0443\\u0433\\u0443\\u044e"}'
        )
        cfg = make_config(take_gone_markers=("срок выплаты истёк",))
        self.assertEqual(_classify_take_response(body, UUID_FREE, cfg), TAKE_REPLY_GONE)


class DumpSpaRequestTest(unittest.TestCase):
    def test_dump_shows_claim_url_and_empty_body(self):
        import httpx

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                log_path=str(Path(tmp) / "actions.log"),
                take_api_url_template="https://911pay.net/trader/payouts/{id}/claim",
                take_payload_format="none",
                take_csrf_header="X-XSRF-TOKEN",
            )
            response = httpx.Response(
                422,
                request=httpx.Request("POST", "https://911pay.net/trader/payouts/x/claim"),
                text='{"message":"Срок выплаты истёк"}',
            )
            fp = _dump_take_request(
                cfg,
                UUID_FREE,
                {},
                response,
                url=cfg.take_api_url_template.replace("{id}", UUID_FREE),
            )
            payload = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["request"]["url"],
                f"https://911pay.net/trader/payouts/{UUID_FREE}/claim",
            )
            self.assertEqual(payload["request"]["post_data"], "")
            self.assertIn("X-XSRF-TOKEN", payload["request"]["headers"])


if __name__ == "__main__":
    unittest.main()
