"""Фильтры взятия заявок: сумма (MINSUM/MAXSUM) и технические заявки «API».

Зачем нужны:

1. Сумма. Трейдеру нужны заявки только в своём диапазоне сумм. Берём заявку,
   лишь если её сумма укладывается в [MINSUM; MAXSUM]. Если фильтр включён, а
   сумму из разметки/JSON достать не удалось, заявка НЕ берётся — лучше
   пропустить, чем взять не ту.

2. Технические заявки разработчиков. Площадка подмешивает в очередь заявки, у
   которых в разметке есть блок со словом «API». Взять их трейдер не может —
   сервер отвечает ошибкой, а бот тратил попытки и слал в Telegram «не взяли»,
   что выглядело как спам. Такие строки отбрасываются ДО POST.

Модуль намеренно не импортирует Config (только читает поля через getattr):
его использует и app/config.py (для разбора чисел из env), и воркеры, поэтому
циклический импорт недопустим.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# Причины пропуска (значения row_skip_reason).
SKIP_REASON_API = "api"
SKIP_REASON_SUM_RANGE = "sum_range"
SKIP_REASON_SUM_UNKNOWN = "sum_unknown"

# Человекочитаемые пояснения для логов.
SKIP_REASON_TEXT = {
    SKIP_REASON_API: "техническая заявка разработчиков (в разметке блок «API»)",
    SKIP_REASON_SUM_RANGE: "сумма вне диапазона MINSUM..MAXSUM",
    SKIP_REASON_SUM_UNKNOWN: "сумма не распознана, а фильтр MINSUM/MAXSUM включён",
}

# Маркер технической заявки по умолчанию (см. .env → TAKE_SKIP_MARKERS).
DEFAULT_SKIP_MARKERS: tuple[str, ...] = ("api",)

# Число: «5000», «5000.00», «1 234,56», «1 234 567.89».
_NUMBER = r"\d+(?:[ \u00a0\u202f']\d{3})*(?:[.,]\d{1,2})?"
# Валюты, которые площадка пишет рядом с суммой.
_CURRENCY = r"(?:rub|rur|usd|eur|uah|kzt|usdt|btc|eth|₽|\$|€|₴|₸)"
_AMOUNT_CURRENCY_RE = re.compile(
    r"(?P<num>" + _NUMBER + r")\s*(?:" + _CURRENCY + r")(?![A-Za-z])", re.I
)
_NUMBER_RE = re.compile(_NUMBER)
# Маркер из латиницы/цифр: границы по ASCII, чтобы «API» ловился рядом с
# кириллицей («API-заявка», «APIзаявка»), но не внутри capital/rapid/is_api.
_ASCII_MARKER_RE = re.compile(r"^[a-z0-9_]+$")

_SPACES = ("\u00a0", "\u202f", "\u2009")



def to_float(raw: Any) -> float | None:
    """Число из строки: «5 000,50», «1,234.56», «5000.00 RUB», «-102.66» → float.

    Понимает оба варианта десятичного разделителя и неразрывные пробелы,
    которыми сайты разделяют тысячи. Возвращает None, если числа нет.
    """
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    for space in _SPACES:
        s = s.replace(space, " ")
    # Убираем валюты, буквы и пробелы — оставляем цифры и разделители.
    s = re.sub(r"[^\d.,\-]", "", s)
    if not re.search(r"\d", s):
        return None
    negative = s.startswith("-")
    s = s.replace("-", "")
    if "," in s and "." in s:
        # Десятичный разделитель — тот, что правее.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        # «1,234» — разделитель тысяч; «1234,56» — десятичный.
        if head and head != "0" and len(tail) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _plain_numbers(text: str) -> list[float]:
    """Числа из текста без привязки к валюте (даты и время отбрасываются)."""
    out: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        start, end = match.span()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        # Даты/время: 2026-11-01, 19:19:21 — это не суммы.
        if after in "-:" and end + 1 < len(text) and text[end + 1].isdigit():
            continue
        if before in "-:" and start >= 2 and text[start - 2].isdigit():
            continue
        value = to_float(match.group(0))
        if value is not None:
            out.append(value)
    return out


def amount_from_text(text: str) -> float | None:
    """Сумма из текста строки: сначала число рядом с валютой, потом — любое.

    Пример строки SPA: «8db375f9-… 5000.00 RUB 2026-11-01 19:19:21» → 5000.0.
    """
    if not text:
        return None
    prepared = text
    for space in _SPACES:
        prepared = prepared.replace(space, " ")
    match = _AMOUNT_CURRENCY_RE.search(prepared)
    if match:
        return to_float(match.group("num"))
    numbers = _plain_numbers(prepared)
    if not numbers:
        return None
    # Предпочитаем число с копейками: «5000.00» точнее, чем «5000».
    for value in numbers:
        if abs(value - int(value)) > 1e-9:
            return value
    return numbers[0]


def parse_amount(value: Any) -> float | None:
    """Сумма из значения JSON: число, строка («5000.00 RUB») или вложенный объект."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "sum", "total"):
            if key in value:
                got = parse_amount(value[key])
                if got is not None:
                    return got
        return None
    if isinstance(value, str):
        return amount_from_text(value)
    return None


@lru_cache(maxsize=64)
def marker_pattern(marker: str) -> re.Pattern | None:
    """Регулярка маркера как ОТДЕЛЬНОГО слова (без учёта регистра).

    Для латинских маркеров границы считаем по ASCII: так «API» находится в
    «API-заявка» и «APIзаявка», но не внутри «capital»/«rapid»/«is_api».
    Для остальных маркеров (кириллица) используем обычные границы слова.
    """
    value = (marker or "").strip()
    if not value:
        return None
    if _ASCII_MARKER_RE.match(value.lower()):
        return re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.I
        )
    return re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)", re.I)


def skip_markers(cfg) -> tuple[str, ...]:
    """Маркеры технических заявок из конфига (cfg=None → дефолт)."""
    if cfg is None:
        return DEFAULT_SKIP_MARKERS
    markers = getattr(cfg, "take_skip_markers", None)
    return tuple(markers) if markers is not None else DEFAULT_SKIP_MARKERS


def has_skip_marker(text: str, cfg) -> bool:
    """Есть ли в тексте строки блок/слово технической заявки (например, «API»)."""
    if not text:
        return False
    for marker in skip_markers(cfg):
        pattern = marker_pattern(marker)
        if pattern is not None and pattern.search(text):
            return True
    return False


def sum_bounds(cfg) -> tuple[float | None, float | None]:
    """(MINSUM, MAXSUM) из конфига; None — граница не проверяется."""
    if cfg is None:
        return (None, None)
    return (getattr(cfg, "min_sum", None), getattr(cfg, "max_sum", None))


def sum_filter_enabled(cfg) -> bool:
    """Задан ли хотя бы один предел суммы."""
    low, high = sum_bounds(cfg)
    return low is not None or high is not None


def sum_allowed(amount: float | None, cfg) -> bool:
    """Укладывается ли сумма в диапазон [MINSUM; MAXSUM].

    Фильтр выключен → True. Включён, а сумма неизвестна → False (не рискуем).
    """
    low, high = sum_bounds(cfg)
    if low is None and high is None:
        return True
    if amount is None:
        return False
    if low is not None and amount < low:
        return False
    if high is not None and amount > high:
        return False
    return True


def row_skip_reason(row: dict, cfg) -> str:
    """Причина, по которой заявку НЕ берём. Пустая строка — можно брать.

    row — элемент, который возвращает parse_rows/_parse_row: там есть «text»
    (текст разметки/JSON для поиска маркеров) и «amount» (распознанная сумма).
    """
    if has_skip_marker(str(row.get("text") or ""), cfg):
        return SKIP_REASON_API
    if not sum_filter_enabled(cfg):
        return ""
    amount = row.get("amount")
    if amount is None:
        return SKIP_REASON_SUM_UNKNOWN
    return "" if sum_allowed(amount, cfg) else SKIP_REASON_SUM_RANGE


def predictive_allowed(cfg) -> bool:
    """Можно ли превентивное взятие «будущих» id.

    При включённом фильтре суммы предиктив запрещён: у предсказанного id нет ни
    разметки, ни суммы, проверить его нечем — можно набрать заявок вне диапазона.
    """
    if not getattr(cfg, "predictive_take", False):
        return False
    return not sum_filter_enabled(cfg)
