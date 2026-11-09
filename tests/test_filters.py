"""Тесты фильтров взятия: сумма (MINSUM/MAXSUM) и технические заявки «API».

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import html as html_lib
import json
import unittest
from types import SimpleNamespace

from app.filters import (
    SKIP_REASON_API,
    SKIP_REASON_SUM_RANGE,
    SKIP_REASON_SUM_UNKNOWN,
    amount_from_text,
    has_skip_marker,
    parse_amount,
    predictive_allowed,
    row_skip_reason,
    sum_allowed,
    sum_filter_enabled,
    to_float,
)
from app.http_worker import parse_rows

UUID_1 = "8db375f9-e7b7-4daa-bef0-36f665497828"


def make_cfg(**overrides) -> SimpleNamespace:
    """Минимальный конфиг для фильтров: они читают поля через getattr."""
    base = dict(
        min_sum=None,
        max_sum=None,
        take_skip_markers=("api",),
        predictive_take=False,
        # поля, которые нужны parse_rows (Inertia/HTML-ветки)
        queue_json_id_field="uuid",
        queue_json_status_field="status",
        queue_json_amount_field="amount",
        row_id_from_first_cell=True,
        payout_row_markers=("payout-row",),
        status_available_markers=("не назначено", "ждет провайдера"),
        status_in_work_markers=("в обработке", "в работе"),
        status_done_markers=("отменена", "истекла", "подтверждена"),
        empty_row_text="не найдено",
        row_id_text_pattern=r"#\s*(\d+)",
        take_id_field="payout_id",
        take_csrf_field="csrf_token",
        take_access_key_field="access_key",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class ToFloatTest(unittest.TestCase):
    """Разбор чисел: пробелы-разделители тысяч и оба вида запятой/точки."""

    def test_plain_and_decimal(self):
        self.assertEqual(to_float("5000.00"), 5000.0)
        self.assertEqual(to_float("750"), 750.0)
        self.assertEqual(to_float("-102.66"), -102.66)

    def test_thousand_separators(self):
        self.assertEqual(to_float("5 000,50"), 5000.5)
        self.assertEqual(to_float("1 234 567.89"), 1234567.89)
        self.assertEqual(to_float("1,234"), 1234.0)
        self.assertEqual(to_float("1,234.56"), 1234.56)
        self.assertEqual(to_float("1.234,56"), 1234.56)

    def test_non_breaking_space_and_currency(self):
        self.assertEqual(to_float("5\u00a0000,00 RUB"), 5000.0)
        self.assertEqual(to_float("RUB 5 000"), 5000.0)

    def test_garbage_returns_none(self):
        for raw in ("", None, "abc", "—"):
            self.assertIsNone(to_float(raw), f"raw={raw!r}")


class AmountFromTextTest(unittest.TestCase):
    """Сумма из текста строки таблицы (SPA: uuid, сумма, дата и время)."""

    def test_spa_row_text(self):
        text = f"{UUID_1} 5000.00 RUB 2026-11-01 19:19:21"
        self.assertEqual(amount_from_text(text), 5000.0)

    def test_ruble_sign(self):
        self.assertEqual(amount_from_text("Взять 1 234,56 ₽"), 1234.56)

    def test_date_and_time_are_not_amounts(self):
        self.assertIsNone(amount_from_text("2026-11-01 19:19:21"))

    def test_plain_sum_without_currency(self):
        self.assertEqual(amount_from_text("Сумма: 750"), 750.0)


class SkipMarkerTest(unittest.TestCase):
    """«API» — техническая заявка разработчиков: ищем как отдельное слово."""

    def test_matches_standalone_word(self):
        cfg = make_cfg()
        self.assertTrue(has_skip_marker("Заявка API #123", cfg))
        self.assertTrue(has_skip_marker("API-заявка", cfg))
        self.assertTrue(has_skip_marker("APIзаявка", cfg))
        self.assertTrue(has_skip_marker("метод api", cfg))

    def test_does_not_match_inside_latin_words(self):
        cfg = make_cfg()
        for text in ("capital", "rapid payout", "is_api true", "graphapi"):
            self.assertFalse(has_skip_marker(text, cfg), text)

    def test_empty_markers_disable_filter(self):
        cfg = make_cfg(take_skip_markers=())
        self.assertFalse(has_skip_marker("Заявка API #123", cfg))

    def test_cyrillic_marker_uses_word_boundaries(self):
        cfg = make_cfg(take_skip_markers=("тест",))
        self.assertTrue(has_skip_marker("тест заявка", cfg))
        self.assertFalse(has_skip_marker("тестовая заявка", cfg))


class SumFilterTest(unittest.TestCase):
    """MINSUM/MAXSUM: границы включительно, неизвестная сумма — не берём."""

    def test_disabled_by_default(self):
        cfg = make_cfg()
        self.assertFalse(sum_filter_enabled(cfg))
        self.assertTrue(sum_allowed(None, cfg))
        self.assertTrue(sum_allowed(-5, cfg))

    def test_min_only(self):
        cfg = make_cfg(min_sum=1000)
        self.assertTrue(sum_allowed(1000, cfg))
        self.assertFalse(sum_allowed(999.99, cfg))
        self.assertFalse(sum_allowed(None, cfg))

    def test_max_only(self):
        cfg = make_cfg(max_sum=5000)
        self.assertTrue(sum_allowed(5000, cfg))
        self.assertFalse(sum_allowed(5000.01, cfg))

    def test_range(self):
        cfg = make_cfg(min_sum=100, max_sum=200)
        self.assertTrue(sum_allowed(150, cfg))
        self.assertFalse(sum_allowed(99, cfg))
        self.assertFalse(sum_allowed(201, cfg))


class RowSkipReasonTest(unittest.TestCase):
    """row_skip_reason: приоритет у «API», затем фильтр суммы."""

    def test_api_has_priority_over_sum(self):
        cfg = make_cfg(min_sum=10_000)
        row = {"id": "u-1", "text": f"{UUID_1} API 100.00 RUB", "amount": 100.0}
        self.assertEqual(row_skip_reason(row, cfg), SKIP_REASON_API)

    def test_sum_unknown_when_filter_on(self):
        cfg = make_cfg(min_sum=1000)
        row = {"id": "u-2", "text": "без суммы", "amount": None}
        self.assertEqual(row_skip_reason(row, cfg), SKIP_REASON_SUM_UNKNOWN)

    def test_sum_out_of_range(self):
        cfg = make_cfg(min_sum=1000, max_sum=5000)
        row = {"id": "u-3", "text": "обычная заявка", "amount": 900.0}
        self.assertEqual(row_skip_reason(row, cfg), SKIP_REASON_SUM_RANGE)

    def test_row_can_be_taken(self):
        cfg = make_cfg(min_sum=1000, max_sum=5000)
        row = {"id": "u-4", "text": "обычная заявка", "amount": 2500.0}
        self.assertEqual(row_skip_reason(row, cfg), "")

    def test_without_filters_and_markers(self):
        cfg = make_cfg()
        self.assertEqual(row_skip_reason({"id": "u-5", "text": "ok"}, cfg), "")


class PredictiveAllowedTest(unittest.TestCase):
    """Предиктив несовместим с фильтром суммы: сумму предсказанного id не проверить."""

    def test_enabled_without_sum_filter(self):
        self.assertTrue(predictive_allowed(make_cfg(predictive_take=True)))

    def test_disabled_by_sum_filter(self):
        cfg = make_cfg(predictive_take=True, min_sum=1000)
        self.assertFalse(predictive_allowed(cfg))

    def test_disabled_when_off(self):
        self.assertFalse(predictive_allowed(make_cfg(predictive_take=False)))


def _inertia_page(rows: list[dict]) -> str:
    """HTML Inertia-страницы: очередь лежит в data-page (props.payouts.data)."""
    payload = {
        "component": "Trader/Payouts/PayoutList",
        "props": {"payouts": {"data": rows}},
    }
    encoded = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return f'<html><body><div id="app" data-page="{encoded}"></div></body></html>'


class InertiaRowsFiltersTest(unittest.TestCase):
    """parse_rows для SPA: сумма и текст заявки попадают в строку очереди."""

    ROWS = [
        {"uuid": "u-api", "status": "unassigned", "amount": 5000, "comment": "тестовая API"},
        {"uuid": "u-small", "status": "unassigned", "amount": 100},
        {"uuid": "u-ok", "status": "unassigned", "amount": "5 000,00 RUB"},
        {"uuid": "u-sum", "status": "unassigned", "sum": 2500},
    ]

    def test_amount_and_text_are_parsed(self):
        rows = {r["id"]: r for r in parse_rows(_inertia_page(self.ROWS), make_cfg())}
        self.assertEqual(rows["u-api"]["amount"], 5000.0)
        self.assertIn("api", rows["u-api"]["text"].lower())
        self.assertEqual(rows["u-ok"]["amount"], 5000.0)
        # поля amount нет — срабатывает типовое имя sum
        self.assertEqual(rows["u-sum"]["amount"], 2500.0)

    def test_custom_amount_field(self):
        cfg = make_cfg(queue_json_amount_field="sum")
        rows = {r["id"]: r for r in parse_rows(_inertia_page(self.ROWS), cfg)}
        self.assertEqual(rows["u-sum"]["amount"], 2500.0)

    def test_filters_reject_api_and_wrong_sum(self):
        cfg = make_cfg(min_sum=1000, max_sum=10_000)
        rows = {r["id"]: r for r in parse_rows(_inertia_page(self.ROWS), cfg)}
        self.assertEqual(row_skip_reason(rows["u-api"], cfg), SKIP_REASON_API)
        self.assertEqual(row_skip_reason(rows["u-small"], cfg), SKIP_REASON_SUM_RANGE)
        self.assertEqual(row_skip_reason(rows["u-ok"], cfg), "")
        self.assertEqual(row_skip_reason(rows["u-sum"], cfg), "")

    def test_parse_amount_handles_numbers_and_dicts(self):
        self.assertEqual(parse_amount(5000), 5000.0)
        self.assertEqual(parse_amount("5 000,00 RUB"), 5000.0)
        self.assertEqual(parse_amount({"amount": "750.50"}), 750.5)
        self.assertIsNone(parse_amount(None))
        self.assertIsNone(parse_amount(True))


class HtmlRowsFiltersTest(unittest.TestCase):
    """parse_rows для HTML-таблицы: сумма берётся из текста строки."""

    HTML = (
        "<table><tbody>"
        '<tr class="payout-row"><td>#7001</td><td>5 000,00 RUB</td>'
        "<td>Ждет провайдера</td></tr>"
        '<tr class="payout-row"><td>#7002</td><td>50.00 RUB</td><td>API</td></tr>'
        "</tbody></table>"
    )

    def test_amount_and_api_marker_from_row_text(self):
        cfg = make_cfg(min_sum=1000)
        rows = {r["id"]: r for r in parse_rows(self.HTML, cfg)}
        self.assertEqual(rows["7001"]["amount"], 5000.0)
        self.assertEqual(rows["7002"]["amount"], 50.0)
        self.assertEqual(row_skip_reason(rows["7001"], cfg), "")
        self.assertEqual(row_skip_reason(rows["7002"], cfg), SKIP_REASON_API)


class SkipReasonForTest(unittest.TestCase):
    """_skip_reason_for (http_worker): фильтры + один лог на id (без спама)."""

    def test_logs_skip_only_once_per_id(self):
        from app.http_worker import _skip_reason_for

        cfg = make_cfg(min_sum=1000)
        row = {"id": "u-log-1", "text": "API", "amount": 5000.0}
        with self.assertLogs("http_worker", level="INFO") as captured:
            self.assertEqual(_skip_reason_for(row, cfg), SKIP_REASON_API)
            # Повторный вызов на том же id не должен писать в лог ещё раз.
            self.assertEqual(_skip_reason_for(row, cfg), SKIP_REASON_API)
        self.assertEqual(len(captured.records), 1)

    def test_allowed_row_returns_empty(self):
        from app.http_worker import _skip_reason_for

        cfg = make_cfg(min_sum=1000)
        row = {"id": "u-log-2", "text": "обычная", "amount": 5000.0}
        self.assertEqual(_skip_reason_for(row, cfg), "")


if __name__ == "__main__":
    unittest.main()

