"""Тесты вспомогательной логики режима живого захвата (app/take_capture.py).

Реальный браузер здесь не нужен: проверяем чистые функции — сборку «рецепта»
HTTP-запроса из перехваченного трафика, маскирование секретов, определение id
заявки и выбор текстов кнопки взятия. Именно эти функции решают, что попадёт
в дамп и какие строки .env предложит инструмент.
"""
import unittest
from types import SimpleNamespace

from app.take_capture import (
    _build_recipe,
    _button_markers,
    _cookie_header,
    _curl_from_recipe,
    _env_lines,
    _extract_order_id,
    _is_interesting_request,
    _looks_like_queue,
    _mask_headers,
    _pick_take_request,
    _template_from_url,
    _truncate,
)


def _cfg():
    return SimpleNamespace(
        payout_take_button_markers=("take_payout",),
        row_id_text_pattern=r"#\s*(\d+)",
    )


class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_truncate("abc", 10), "abc")

    def test_none_stays_none(self):
        self.assertIsNone(_truncate(None))

    def test_long_text_marked(self):
        out = _truncate("x" * 20, 5)
        self.assertTrue(out.startswith("xxxxx"))
        self.assertIn("обрезано", out)
        self.assertIn("20", out)


class LooksLikeQueueTest(unittest.TestCase):
    def test_spa_marker_from_config(self):
        cfg = SimpleNamespace(queue_page_markers=("payouts-compact",))
        self.assertTrue(_looks_like_queue("<div class='payouts-compact'>", cfg))

    def test_real_spa_marker_without_config(self):
        # Реальная разметка 911pay: <table class="w-full payouts-compact">
        cfg = SimpleNamespace(queue_page_markers=())
        self.assertTrue(_looks_like_queue("<table class='w-full payouts-compact'>", cfg))

    def test_old_cabinet_marker_still_works(self):
        cfg = SimpleNamespace(queue_page_markers=())
        self.assertTrue(_looks_like_queue("<table class='payout-table'>", cfg))

    def test_login_page_is_not_queue(self):
        cfg = SimpleNamespace(queue_page_markers=("payouts-compact",))
        self.assertFalse(_looks_like_queue("<form class='auth-form' id='email'>", cfg))


class MaskHeadersTest(unittest.TestCase):
    def test_cookie_and_auth_masked(self):
        masked = _mask_headers({"Cookie": "a=b", "Authorization": "Bearer z", "X-Test": "1"})
        self.assertIn("скрыто", masked["Cookie"])
        self.assertIn("скрыто", masked["Authorization"])
        self.assertEqual(masked["X-Test"], "1")


class ButtonMarkersTest(unittest.TestCase):
    def test_merges_cfg_and_extra(self):
        words = _button_markers(_cfg(), extra="Забрать, CUSTOM")
        self.assertIn("взять", words)
        self.assertIn("take_payout", words)
        self.assertIn("забрать", words)
        self.assertIn("custom", words)

    def test_deduplicates(self):
        words = _button_markers(_cfg(), extra="ВЗЯТЬ")
        self.assertEqual(words.count("взять"), 1)


class InterestingRequestTest(unittest.TestCase):
    def test_post_always_interesting(self):
        self.assertTrue(_is_interesting_request("POST", "document", "https://x/form"))

    def test_get_xhr_with_marker(self):
        self.assertTrue(
            _is_interesting_request("GET", "xhr", "https://x/api/payouts/payout-list")
        )

    def test_get_image_not_interesting(self):
        self.assertFalse(_is_interesting_request("GET", "image", "https://x/logo.png"))

    def test_get_xhr_without_marker_not_interesting(self):
        self.assertFalse(_is_interesting_request("GET", "xhr", "https://x/config.js"))


class ExtractOrderIdTest(unittest.TestCase):
    def test_hash_pattern(self):
        self.assertEqual(_extract_order_id("# 6408416 Не назначено", _cfg()), "6408416")

    def test_plain_number_for_spa(self):
        self.assertEqual(_extract_order_id("6408416 Не назначено 1 200", _cfg()), "6408416")

    def test_no_number(self):
        self.assertIsNone(_extract_order_id("Заявок нет", _cfg()))

    def test_broken_pattern_falls_back(self):
        cfg = SimpleNamespace(row_id_text_pattern="(")
        self.assertEqual(_extract_order_id("id 12345", cfg), "12345")

    def test_extracts_uuid_for_911pay(self):
        """SPA 911pay: id заявки — UUID, а в тексте ещё есть суммы/время."""
        text = "8db375f9-e7b7-4daa-bef0-36f665497828 5000.00 RUB 2026-11-01 19:19:21"
        self.assertEqual(
            _extract_order_id(text, _cfg()), "8db375f9-e7b7-4daa-bef0-36f665497828"
        )

    def test_template_replaces_uuid_segment(self):
        self.assertEqual(
            _template_from_url(
                "https://911pay.net/trader/payouts/8db375f9-e7b7-4daa-bef0-36f665497828/claim"
            ),
            "https://911pay.net/trader/payouts/{id}/claim",
        )


class TemplateFromUrlTest(unittest.TestCase):
    def test_replaces_known_id(self):
        self.assertEqual(
            _template_from_url("https://x/api/payouts/6408416/take", "6408416"),
            "https://x/api/payouts/{id}/take",
        )

    def test_replaces_last_numeric_segment(self):
        self.assertEqual(
            _template_from_url("https://x/api/payouts/9999/take"),
            "https://x/api/payouts/{id}/take",
        )

    def test_keeps_url_without_numbers(self):
        self.assertEqual(_template_from_url("https://x/api/take"), "https://x/api/take")


class PickTakeRequestTest(unittest.TestCase):
    def test_prefers_take_url(self):
        entries = [
            {"kind": "request", "method": "POST", "url": "https://x/api/other"},
            {"kind": "request", "method": "POST", "url": "https://x/api/payouts/1/take"},
        ]
        self.assertEqual(_pick_take_request(entries)["url"], "https://x/api/payouts/1/take")

    def test_none_when_only_get(self):
        entries = [{"kind": "request", "method": "GET", "url": "https://x/api/payouts"}]
        self.assertIsNone(_pick_take_request(entries))


class BuildRecipeTest(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {
                "kind": "request",
                "method": "POST",
                "url": "https://x/api/payouts/6408416/take",
                "headers": {"content-type": "application/json", "cookie": "s=1"},
                "post_data": '{"id": 6408416}',
            },
            {
                "kind": "response",
                "url": "https://x/api/payouts/6408416/take",
                "status": 200,
                "headers": {"content-type": "application/json", "set-cookie": "s=1"},
                "body": '{"ok": true}',
            },
        ]

    def test_recipe_detects_json_and_id_field(self):
        recipe = _build_recipe(self.entries, "6408416")
        self.assertEqual(recipe["method"], "POST")
        self.assertEqual(recipe["payload_format"], "json")
        self.assertEqual(recipe["id_field"], "id")
        self.assertEqual(recipe["url_template"], "https://x/api/payouts/{id}/take")
        self.assertEqual(recipe["response_status"], 200)
        self.assertIn("ok", recipe["response_body"])

    def test_recipe_without_csrf_header(self):
        recipe = _build_recipe(self.entries, "6408416")
        self.assertEqual(recipe["csrf_header"], "")
        self.assertIn("TAKE_REQUIRES_CSRF=false", _env_lines(recipe))

    def test_form_payload_detected(self):
        entries = [
            {
                "kind": "request",
                "method": "POST",
                "url": "https://x/payouts",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
                "post_data": "payout_id=6408416&act=take_payout",
            }
        ]
        recipe = _build_recipe(entries, "6408416")
        self.assertEqual(recipe["payload_format"], "form")
        self.assertIsNone(recipe["id_field"])  # не JSON — поле не угадываем

    def test_no_requests_gives_empty_recipe(self):
        self.assertEqual(_build_recipe([], None), {})


class EnvLinesTest(unittest.TestCase):
    def test_contains_key_settings(self):
        recipe = {
            "url_template": "https://x/api/payouts/{id}/take",
            "payload_format": "json",
            "id_field": "id",
            "csrf_header": "",
        }
        lines = _env_lines(recipe)
        self.assertIn("TAKE_API_URL_TEMPLATE=https://x/api/payouts/{id}/take", lines)
        self.assertIn("TAKE_PAYLOAD_FORMAT=json", lines)
        self.assertIn("TAKE_ID_FIELD=id", lines)
        self.assertIn("TAKE_REQUIRES_CSRF=false", lines)

    def test_csrf_true_when_header_present(self):
        recipe = {"csrf_header": "x-csrf-token"}
        self.assertIn("TAKE_REQUIRES_CSRF=true", _env_lines(recipe))

    def test_empty_recipe(self):
        self.assertEqual(_env_lines({}), [])


class CurlTest(unittest.TestCase):
    def test_cookie_not_duplicated_in_headers(self):
        recipe = {
            "method": "POST",
            "url": "https://x/api/payouts/1/take",
            "request_headers": {"content-type": "application/json", "cookie": "s=1"},
            "body": '{"id": 1}',
        }
        curl = _curl_from_recipe(recipe, cookie="s=1")
        self.assertIn("curl -X POST", curl)
        self.assertIn("--data-raw", curl)
        self.assertIn("Cookie: s=1", curl)
        # cookie из заголовков не должна попасть второй раз
        self.assertEqual(curl.count("Cookie: s=1"), 1)

    def test_empty_recipe_gives_empty_curl(self):
        self.assertEqual(_curl_from_recipe({}), "")


class CookieHeaderTest(unittest.TestCase):
    def test_filters_foreign_domains(self):
        cookies = [
            {"name": "a", "value": "1", "domain": ".example.com"},
            {"name": "b", "value": "2", "domain": ".other.com"},
        ]
        header = _cookie_header(cookies, "https://example.com/queue")
        self.assertEqual(header, "a=1")

    def test_empty_cookies(self):
        self.assertEqual(_cookie_header([], "https://example.com"), "")


if __name__ == "__main__":
    unittest.main()
