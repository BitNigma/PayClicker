"""Режим «обучения взятию»: живой (headful) браузер вместо HTTP-цикла.

Зачем этот файл
---------------
WORKER_BACKEND=http работает быстро, но требует ТОЧНО знать запрос взятия
заявки. Пока API площадки неизвестен (SPA на Vue, взятие уходит fetch/POST'ом
с JSON, а не HTML-формой), взять заявку можно только кликом по кнопке в
настоящем браузере. После браузерной авторизации обычный путь (login_via_browser)
закрывает Chromium — окно исчезает, и посмотреть/нажать кнопку негде.

Этот инструмент решает именно это:
  1. поднимает ОДИН headful Chromium с уже сохранённой сессией
     (account.storage_state_path, по умолчанию ./data/auth.json);
  2. сам обновляет страницу очереди (автообновление — страховка на случай,
     если SPA не дёргает DOM сам);
  3. как только на странице появляется ВИДИМАЯ и включённая кнопка взятия —
     кликает её (клик как страховочный вариант взятия, пока нет API);
  4. перехватывает РЕАЛЬНЫЙ сетевой запрос и ответ сервера (URL, метод,
     заголовки, тело, статус, тело ответа) и складывает их в
     ./data/take_capture/*.json (права 600 — там cookie/токены);
  5. печатает готовый «рецепт»: TAKE_API_URL_TEMPLATE / TAKE_PAYLOAD_FORMAT /
     TAKE_ID_FIELD + curl, чтобы затем воспроизвести взятие чистым HTTP.

То есть: пока API не изучен — бот берёт заявки кликом, а каждый клик даёт
данные, чтобы перейти на быстрый HTTP-режим и снова работать без окна.

Запуск
------
    python -m app.take_capture                  # headful, обновление 3с, 1 взятие
    python -m app.take_capture --max-takes 0    # непрерывно кликать (страховка)
    python -m app.take_capture --no-click       # только смотреть/обновлять
    python -m app.take_capture --headless       # без окна (для проверки)

ВАЖНО: инструмент использует ТУ ЖЕ сессию, что и бот. Не запускайте его
одновременно с WORKER_BACKEND=http в режиме взятия — вы будете конкурировать
сами с собой за одни и те же заявки. Сначала /stop в Telegram, потом сюда.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, async_playwright

# Прямой запуск файла (`python app/take_capture.py`) не объявляет пакет —
# добавляем корень проекта в sys.path, чтобы относительные импорты работали.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "app"  # noqa: A001

from .browser_worker import (  # noqa: E402
    _build_context,
    _is_challenge_page,
    _looks_like_cabinet,
    _on_signin,
    _register_dialog_autoaccept,
    _wait_challenge_passed,
    _wait_url_settle,
)
from .config import Config, load_config  # noqa: E402

logger = logging.getLogger("take_capture")

# Тексты кнопки взятия. У SPA кнопка обычно подписана «Взять»/«Забрать»;
# английские варианты — на случай локали сайта. Дополнительно подмешиваются
# PAYOUT_TAKE_BUTTON_MARKERS из .env и --button-text.
DEFAULT_BUTTON_WORDS = (
    "взять",
    "забрать",
    "принять",
    "take payout",
    "take order",
    "get payout",
    "claim",
)

# Какие запросы считаем «интересными» для обучения API. GET очереди тоже
# полезен (эндпоинт опроса), но берём его только для xhr/fetch по этим словам.
INTERESTING_URL_MARKERS = (
    "payout",
    "take",
    "claim",
    "order",
    "demand",
    "queue",
    "api",
)

MAX_BODY_CHARS = 20000  # тело ответа в дампе — с запасом, но без гигантских HTML


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _truncate(text, limit: int = MAX_BODY_CHARS) -> str | None:
    """Обрезает длинный текст, помечая фактическую длину."""
    if text is None:
        return None
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…<обрезано, всего {len(s)} симв.>"


def _mask_headers(headers: dict) -> dict:
    """Заголовки для ПЕЧАТИ в консоль: cookie/authorization скрываем.

    В JSON-дамп заголовки пишутся как есть (файл с правами 600) — иначе по ним
    нельзя воспроизвести запрос. В консоли секреты не нужны.
    """
    out = {}
    for key, value in (headers or {}).items():
        low = str(key).lower()
        if low in ("cookie", "set-cookie", "authorization"):
            out[key] = f"<скрыто, {len(str(value))} симв.>"
        else:
            out[key] = _truncate(str(value), 300)
    return out


def _button_markers(cfg: Config, extra: str = "") -> tuple:
    """Тексты кнопки взятия: дефолт + PAYOUT_TAKE_BUTTON_MARKERS + --button-text."""
    words: list[str] = []
    for raw in DEFAULT_BUTTON_WORDS + tuple(getattr(cfg, "payout_take_button_markers", ()) or ()):
        w = str(raw).strip().lower()
        if w and w not in words:
            words.append(w)
    for part in (extra or "").split(","):
        w = part.strip().lower()
        if w and w not in words:
            words.append(w)
    return tuple(words)


def _is_interesting_request(method: str, resource_type: str, url: str) -> bool:
    """Ловим только то, что нужно для обучения API взятия.

    Любой не-GET (POST/PUT/PATCH/DELETE) — всегда интересен. GET берём только
    как xhr/fetch и только если в URL есть предметные слова (payout/take/…):
    иначе в дамп попадут картинки, шрифты и сотни служебных запросов.
    """
    method = (method or "GET").upper()
    if method != "GET":
        return True
    if (resource_type or "").lower() in ("xhr", "fetch"):
        low = (url or "").lower()
        return any(m in low for m in INTERESTING_URL_MARKERS)
    return False


def _parse_body(body: str):
    """Тело запроса → Python-объект (если это JSON), иначе None."""
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _header(headers: dict, name: str) -> str:
    low = (name or "").lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == low:
            return str(value)
    return ""


def _find_csrf_header(headers: dict) -> str:
    """Имя заголовка с CSRF/XSRF-токеном, если он реально уходит в запросе."""
    for key in headers or {}:
        low = str(key).lower()
        if "csrf" in low or "xsrf" in low:
            return str(key)
    return ""


UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _extract_order_id(row_text: str, cfg: Config) -> str | None:
    """id заявки из текста строки: ROW_ID_TEXT_PATTERN → UUID → число.

    У SPA id лежит обычным текстом в первой ячейке. Для 911pay это UUID
    (`8db375f9-…`), для старых площадок — число («6408416» без «#»), поэтому
    после конфигурируемого шаблона проверяем UUID, затем первое длинное число.
    """
    if not row_text:
        return None
    pattern = getattr(cfg, "row_id_text_pattern", r"#\s*(\d+)") or r"#\s*(\d+)"
    try:
        match = re.search(pattern, row_text)
        if match:
            return match.group(1)
    except re.error:
        pass
    match = UUID_RE.search(row_text)
    if match:
        return match.group(0)
    match = re.search(r"\b(\d{4,})\b", row_text)
    return match.group(1) if match else None


def _template_from_url(url: str, order_id: str | None = None) -> str:
    """URL запроса → шаблон с {id} (для TAKE_API_URL_TEMPLATE).

    Сначала пробуем заменить сам id заявки; если его в URL нет — заменяем
    последний числовой сегмент пути длиной 4+ цифры (типовой REST-вид).
    """
    if not url:
        return ""
    if order_id and str(order_id) in url:
        return url.replace(str(order_id), "{id}")
    parts = urlsplit(url)
    segments = parts.path.split("/")
    for i in range(len(segments) - 1, -1, -1):
        seg = segments[i]
        # UUID (911pay) или длинный числовой сегмент (REST-площадки).
        if UUID_RE.fullmatch(seg) or (seg.isdigit() and len(seg) >= 4):
            segments[i] = "{id}"
            return parts._replace(path="/".join(segments)).geturl()
    return url


def _pick_take_request(entries: list[dict]) -> dict | None:
    """Выбирает запрос, который с наибольшей вероятностью и есть «взятие».

    Приоритет: не-GET с предметными словами в URL → любой не-GET → None.
    """
    requests = [e for e in entries if e.get("kind") == "request" and e.get("method") != "GET"]
    if not requests:
        return None
    for entry in requests:
        low = (entry.get("url") or "").lower()
        if any(m in low for m in ("take", "claim", "payout", "order")):
            return entry
    return requests[0]


def _response_for(entries: list[dict], url: str) -> dict | None:
    """Ответ, снятый с того же URL, что и запрос (для рецепта)."""
    for entry in entries:
        if entry.get("kind") == "response" and entry.get("url") == url:
            return entry
    return None


def _build_recipe(entries: list[dict], order_id: str | None = None) -> dict:
    """Из перехваченного трафика собирает «рецепт» для HTTP-режима.

    Возвращает пустой dict, если запросов взятия не было (например, клик не
    состоялся): тогда в дампе остаётся только сырой трафик для ручного разбора.
    """
    req = _pick_take_request(entries)
    if not req:
        return {}
    url = req.get("url") or ""
    body = req.get("post_data") or ""
    data = _parse_body(body)
    headers = req.get("headers") or {}
    content_type = _header(headers, "content-type")
    payload_format = (
        "json" if isinstance(data, (dict, list)) and "json" in content_type.lower() else "form"
    )

    id_field = None
    if isinstance(data, dict):
        for key, value in data.items():
            if order_id and str(value) == str(order_id):
                id_field = key
                break
        if not id_field:
            for key in data:
                if "id" in str(key).lower():
                    id_field = key
                    break

    resp = _response_for(entries, url) or {}
    return {
        "method": req.get("method"),
        "url": url,
        "url_template": _template_from_url(url, order_id),
        "payload_format": payload_format,
        "id_field": id_field,
        "content_type": content_type,
        "csrf_header": _find_csrf_header(headers),
        "request_headers": headers,
        "body": _truncate(body, 4000),
        "response_status": resp.get("status"),
        "response_headers": _mask_headers(resp.get("headers") or {}),
        "response_body": _truncate(resp.get("body"), 4000),
    }


def _env_lines(recipe: dict) -> list[str]:
    """Строки для .env, которые сразу превращают взятие в HTTP-запрос."""
    if not recipe:
        return []
    lines: list[str] = []
    if recipe.get("url_template"):
        lines.append(f"TAKE_API_URL_TEMPLATE={recipe['url_template']}")
    if recipe.get("payload_format"):
        lines.append(f"TAKE_PAYLOAD_FORMAT={recipe['payload_format']}")
    if recipe.get("id_field"):
        lines.append(f"TAKE_ID_FIELD={recipe['id_field']}")
    # У SPA csrf-токена в HTML нет — он либо в cookie, либо в заголовке.
    lines.append("TAKE_REQUIRES_CSRF=" + ("true" if recipe.get("csrf_header") else "false"))
    return lines


def _curl_from_recipe(recipe: dict, cookie: str = "") -> str:
    """Готовый curl для ручной проверки (cookie подставляется из сессии)."""
    if not recipe or not recipe.get("url"):
        return ""
    parts = [f"curl -X {recipe.get('method') or 'POST'} '{recipe['url']}'"]
    headers = dict(recipe.get("request_headers") or {})
    # Cookie подставляем отдельной строкой; служебные заголовки клиента не нужны.
    for key in list(headers):
        if str(key).lower() in ("cookie", "content-length", "host", "connection", "accept-encoding"):
            headers.pop(key)
    for key, value in headers.items():
        safe = str(value).replace("'", "'\\''")
        parts.append(f"  -H '{key}: {safe}'")
    if cookie:
        safe_cookie = cookie.replace("'", "'\\''")
        parts.append(f"  -H 'Cookie: {safe_cookie}'")
    body = recipe.get("body")
    if body:
        safe_body = str(body).replace("'", "'\\''")
        parts.append(f"  --data-raw '{safe_body}'")
    return " \\\n".join(parts)


# JS-сканер кнопок взятия. Выполняется В САМОЙ странице (Vue-разметка неизвестна,
# поэтому ищем по тексту, а не по классу), помечает найденную кнопку атрибутом
# data-ob-take-target, чтобы затем кликнуть её обычным селектором Playwright.
SCAN_JS = """
(words) => {
  document.querySelectorAll('[data-ob-take-target]')
    .forEach(el => el.removeAttribute('data-ob-take-target'));
  const nodes = Array.from(document.querySelectorAll(
    'button, a, [role="button"], input[type="submit"], input[type="button"]'
  ));
  const candidates = [];
  let chosen = null;
  for (const el of nodes) {
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim();
    if (!text) continue;
    const low = text.toLowerCase();
    if (!words.some(w => low.includes(w))) continue;
    const st = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const visible = st.display !== 'none' && st.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
    const disabled = el.disabled === true
      || el.getAttribute('aria-disabled') === 'true'
      || el.classList.contains('disabled')
      || st.pointerEvents === 'none';
    const row = el.closest('tr, li, [class*="row"], [class*="card"], [class*="item"], [class*="payout"]');
    const rowText = row ? (row.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400) : '';
    const info = {text: text.slice(0, 80), visible: visible, disabled: disabled, rowText: rowText};
    candidates.push(info);
    if (!chosen && visible && !disabled) {
      chosen = info;
      el.setAttribute('data-ob-take-target', '1');
    }
  }
  return {
    found: !!chosen,
    chosen: chosen,
    total: candidates.length,
    candidates: candidates.slice(0, 10)
  };
}
"""

TAKE_TARGET_SELECTOR = '[data-ob-take-target="1"]'


async def _scan_take_target(page: Page, markers: tuple) -> dict:
    """Ищет на странице готовую к клику кнопку взятия (best-effort)."""
    try:
        return await page.evaluate(SCAN_JS, list(markers))
    except Exception as exc:
        logger.warning("Скан кнопки взятия не удался: %s", exc)
        return {"found": False, "chosen": None, "total": 0, "candidates": []}


async def _click_take_target(page: Page, timeout_ms: int = 5000) -> bool:
    """Кликает помеченную кнопку взятия. Ошибки не пробрасываем."""
    try:
        await page.click(TAKE_TARGET_SELECTOR, timeout=timeout_ms)
        return True
    except Exception as exc:
        logger.warning("Клик по кнопке взятия не удался: %s", exc)
        return False


class _CaptureSink:
    """Собирает сетевые запросы/ответы страницы для последующего разбора.

    Обработчики Playwright синхронные, поэтому тело ответа читается отдельной
    задачей (res.text() — async); к моменту сохранения дампа задачи дожидаются
    через flush().
    """

    def __init__(self, max_body_chars: int = MAX_BODY_CHARS):
        self.entries: list[dict] = []
        self.max_body_chars = max_body_chars
        self._pending: set[asyncio.Task] = set()

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, req) -> None:
        try:
            if not _is_interesting_request(req.method, req.resource_type, req.url):
                return
            self.entries.append(
                {
                    "kind": "request",
                    "ts": _now_iso(),
                    "method": req.method,
                    "url": req.url,
                    "resource_type": req.resource_type,
                    "headers": dict(req.headers),
                    "post_data": _truncate(req.post_data, self.max_body_chars),
                }
            )
        except Exception:
            logger.exception("Не удалось записать request")

    def _on_response(self, res) -> None:
        try:
            req = res.request
            if not _is_interesting_request(req.method, req.resource_type, req.url):
                return
            entry = {
                "kind": "response",
                "ts": _now_iso(),
                "method": req.method,
                "url": res.url,
                "status": res.status,
                "headers": {},
                "body": None,
            }
            self.entries.append(entry)
            task = asyncio.create_task(self._fill_response(entry, res))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except Exception:
            logger.exception("Не удалось записать response")

    async def _fill_response(self, entry: dict, res) -> None:
        try:
            entry["headers"] = dict(await res.all_headers())
        except Exception:
            pass
        try:
            entry["body"] = _truncate(await res.text(), self.max_body_chars)
        except Exception as exc:
            entry["body_error"] = str(exc)

    async def flush(self, timeout: float = 3.0) -> None:
        """Дожидается чтения тел ответов (или таймаута — дамп всё равно пишем)."""
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(self._pending), return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Не все тела ответов успели прочитаться за %.1f c", timeout)


async def _dump_capture(
    page: Page,
    entries: list[dict],
    order_id: str | None,
    out_dir: Path,
) -> Path:
    """Сохраняет перехваченный трафик + HTML/скриншот страницы в ./data.

    JSON содержит cookie/токены, поэтому файлу выставляются права 600.
    HTML и PNG нужны, когда рецепт не собрался: по ним видно, что было на
    странице и какие поля/кнопки реально существуют.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"take_{order_id or 'unknown'}_{stamp}"
    payload = {
        "captured_at": _now_iso(),
        "page_url": page.url,
        "order_id": order_id,
        "recipe": _build_recipe(entries, order_id),
        "entries": entries,
    }
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(json_path, 0o600)
    except OSError:
        pass

    # HTML/скриншот — вспомогательные: их сбой не должен ломать захват.
    try:
        base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        logger.exception("Не удалось сохранить HTML страницы")
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        logger.exception("Не удалось сохранить скриншот")
    return json_path


def _cookie_header(cookies: list[dict], url: str) -> str:
    """Cookie-заголовок из context.cookies() для конкретного URL (для curl)."""
    host = (urlsplit(url).hostname or "").lower()
    parts = []
    for cookie in cookies or []:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if host and domain and not (host == domain or host.endswith("." + domain)):
            continue
        parts.append(f"{cookie.get('name')}={cookie.get('value')}")
    return "; ".join(parts)


def _print_capture(
    path: Path, entries: list[dict], order_id: str | None, cookie: str = ""
) -> None:
    """Печатает краткую сводку и готовый рецепт для HTTP-режима."""
    recipe = _build_recipe(entries, order_id)
    print("\n" + "=" * 78)
    print(f"📸 Захват сохранён: {path}")
    if order_id:
        print(f"Заявка: #{order_id}")
    if not recipe:
        print(
            "⚠️ Запрос взятия не пойман: клик не состоялся или сайт сделал что-то\n"
            "   нестандартное. В дампе остался весь интересный трафик — пришлите\n"
            "   JSON/HTML, подберём селектор или запрос вручную."
        )
        print("=" * 78 + "\n")
        return

    print(f"\nЗапрос: {recipe['method']} {recipe['url']}")
    print(f"Статус ответа: {recipe.get('response_status')}")
    print(f"Формат тела: {recipe['payload_format']}, поле id: {recipe.get('id_field')}")
    if recipe.get("body"):
        print(f"Тело запроса: {recipe['body']}")
    if recipe.get("response_body"):
        print(f"Ответ (начало): {_truncate(recipe['response_body'], 800)}")
    print("\nГотовые строки для .env:")
    for line in _env_lines(recipe):
        print("  " + line)
    curl = _curl_from_recipe(recipe, cookie=cookie)
    if curl:
        print("\nПроверка вручную (cookie уже подставлена из сессии):")
        print(curl)
    print("=" * 78 + "\n")


def _looks_like_queue(html: str, cfg: Config) -> bool:
    """Это страница очереди выплат (а не форма входа/капча)?

    _looks_like_cabinet() знает старую разметку (payout-table/payout-card/
    payout-status/user-account-page) и SPA-контейнер `payouts-compact`; сверх
    этого здесь учитываются маркеры площадки из `QUEUE_PAGE_MARKERS`. Без такой
    проверки рабочая SPA-страница считалась «не кабинетом», и инструмент уходил
    в ожидание ручного входа до таймаута.
    """
    low = (html or "").lower()
    markers = tuple(getattr(cfg, "queue_page_markers", ()) or ())
    if any(str(m).lower() in low for m in markers if str(m).strip()):
        return True
    return _looks_like_cabinet(html, cfg)


async def _wait_manual_login(page: Page, cfg: Config, timeout_s: float) -> bool:
    """Ждёт, пока пользователь войдёт вручную в УЖЕ ОТКРЫТОМ окне браузера.

    Это принципиально: раньше после авто-логина Chromium закрывался, и посмотреть
    /нажать кнопку было негде. Здесь окно остаётся видимым, а инструмент лишь
    раз в секунду проверяет, что страница стала кабинетом (тогда — продолжаем).
    """
    print("\n⚠️ Сохранённая сессия не активна — открылась страница входа.")
    print("   Войдите ВРУЧНУЮ в открытом окне: email → «Далее» → пароль → 2FA.")
    print(f"   Жду до {int(timeout_s)} c (проверяю страницу каждую секунду)…")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            html = await page.content()
        except Exception:
            html = ""
        if _looks_like_queue(html, cfg):
            print("✅ Вход выполнен — продолжаю в том же окне.")
            return True
        if _is_challenge_page(html):
            print("⏳ Cloudflare-проверка «Один момент…» — пережидаю.")
        await asyncio.sleep(1)
    print("❌ Вход не выполнен за отведённое время.")
    return False


async def _store_session(context, account) -> None:
    """Сохраняет cookies, чтобы HTTP-режим потом подхватил эту же сессию."""
    try:
        Path(account.storage_state_path).parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=account.storage_state_path)
        print(f"💾 Сессия сохранена: {account.storage_state_path}")
    except Exception as exc:
        logger.warning("Не удалось сохранить сессию: %s", exc)


async def _wait_after_capture() -> None:
    """Держит окно открытым, пока пользователь не нажмёт Ctrl+C.

    Нужно, чтобы можно было глазами посмотреть состояние страницы после клика
    (и убедиться, что заявка действительно ушла в работу).
    """
    print("Окно браузера остаётся открытым. Закройте его вручную или нажмите Ctrl+C.")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


async def run_capture(cfg: Config, account, opts) -> int:
    """Главный цикл: видимое окно → автообновление → клик → захват трафика."""
    cfg.headless = bool(opts.headless)  # по умолчанию окно видно (как просили)
    markers = _button_markers(cfg, opts.button_text)
    out_dir = Path(opts.out) if opts.out else Path(cfg.log_path).parent / "take_capture"
    sink = _CaptureSink()

    async def notify(text: str) -> None:
        print(text)

    async with async_playwright() as pw:
        browser, context = await _build_context(pw, cfg, account)
        page = await context.new_page()
        _register_dialog_autoaccept(page)
        sink.attach(page)
        try:
            print(f"🌐 Открываю {cfg.queue_url} (headless={cfg.headless})")
            await page.goto(cfg.queue_url, wait_until="domcontentloaded")
            await _wait_url_settle(page)
            try:
                html = await page.content()
            except Exception:
                html = ""
            # SPA рисует таблицу JS-ом: сразу после domcontentloaded маркеров в
            # HTML ещё нет. Даём странице до 15 c, иначе рабочую очередь можно
            # принять за форму входа и уйти в бессмысленное ожидание.
            render_deadline = time.monotonic() + 15
            while (
                time.monotonic() < render_deadline
                and not _looks_like_queue(html, cfg)
                and not _on_signin(page, cfg)
                and not _is_challenge_page(html)
            ):
                await asyncio.sleep(0.5)
                try:
                    html = await page.content()
                except Exception:
                    html = ""
            if _is_challenge_page(html):
                print("⏳ Cloudflare-проверка «Один момент…» — пережидаю.")
                await _wait_challenge_passed(page, cfg, notify, cfg.login_form_timeout_s)
                try:
                    html = await page.content()
                except Exception:
                    html = ""

            if _on_signin(page, cfg) or not _looks_like_queue(html, cfg):
                if not await _wait_manual_login(page, cfg, opts.login_wait):
                    return 2
                await _store_session(context, account)
                await page.goto(cfg.queue_url, wait_until="domcontentloaded")
                await _wait_url_settle(page)

            takes = 0
            reloads = 0
            clicked_rows: set[str] = set()
            last_reload = time.monotonic()
            deadline = time.monotonic() + opts.timeout
            limit = "∞" if opts.max_takes <= 0 else str(opts.max_takes)
            print(
                f"▶️ Обновление каждые {opts.refresh:g} c; тексты кнопки: {', '.join(markers)}; "
                + ("только наблюдение" if opts.no_click else f"лимит взятий: {limit}")
            )

            while time.monotonic() < deadline:
                if _on_signin(page, cfg):
                    print("⚠️ Сессия слетела — останавливаюсь. Перезапустите и войдите заново.")
                    break

                scan = await _scan_take_target(page, markers)
                chosen = scan.get("chosen")
                can_take = bool(chosen) and not opts.no_click and (
                    opts.max_takes <= 0 or takes < opts.max_takes
                )
                if can_take:
                    row_text = str(chosen.get("rowText") or "").strip()
                    order_id = _extract_order_id(row_text, cfg)
                    # Ключ дедупликации — id заявки (надёжнее текста строки: статус
                    # в тексте меняется, а id нет). Без id — текст строки/кнопки.
                    key = order_id or row_text or str(chosen.get("text") or "")
                    if key and key in clicked_rows:
                        can_take = False  # эту заявку уже брали — повторно не жмём
                if can_take:
                    suffix = f" (заявка #{order_id})" if order_id else ""
                    print(f"🖱️ Кликаю «{chosen.get('text')}»{suffix}…")
                    before = len(sink.entries)
                    if await _click_take_target(page):
                        # Даём сайту время отправить fetch/POST и получить ответ.
                        await asyncio.sleep(opts.click_wait)
                        await sink.flush()
                        entries = sink.entries[before:]
                        if not entries:
                            print("ℹ️ Новых сетевых запросов после клика нет — смотрите окно браузера.")
                        path = await _dump_capture(page, entries, order_id, out_dir)
                        cookies = []
                        try:
                            cookies = await context.cookies()
                        except Exception:
                            pass
                        _print_capture(
                            path, entries, order_id, _cookie_header(cookies, cfg.queue_url)
                        )
                        takes += 1
                        if key:
                            clicked_rows.add(key)
                        if opts.max_takes > 0 and takes >= opts.max_takes:
                            print(
                                f"⏹ Лимит взятий ({opts.max_takes}) достигнут — "
                                "больше не кликаю, окно оставляю открытым для проверки."
                            )
                            break
                        # После клика обновляем страницу: статус строки мог измениться.
                        try:
                            await page.reload(wait_until="domcontentloaded")
                        except Exception:
                            pass
                        last_reload = time.monotonic()
                        continue

                if time.monotonic() - last_reload >= opts.refresh:
                    try:
                        await page.reload(wait_until="domcontentloaded")
                        last_reload = time.monotonic()
                        reloads += 1
                        if reloads == 1 or reloads % 10 == 0:
                            print(f"🔄 Обновление страницы №{reloads}: кнопки взятия пока нет")
                    except Exception as exc:
                        print(f"⚠️ Не удалось обновить страницу: {exc}")
                await asyncio.sleep(0.3)

            print(f"\nИтог: взятий {takes}, обновлений {reloads}.")
            if takes == 0:
                # Ничего не поймали — сохраняем страницу, чтобы можно было подобрать
                # селектор кнопки/строки по реальной разметке.
                snap = out_dir / f"watch_{time.strftime('%Y%m%d_%H%M%S')}.html"
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    snap.write_text(await page.content(), encoding="utf-8")
                    print(f"HTML текущей страницы: {snap}")
                except Exception:
                    logger.exception("Не удалось сохранить HTML наблюдения")
            if not opts.close and not cfg.headless:
                await _wait_after_capture()
            return 0
        finally:
            # Сессию сохраняем только если мы не на странице входа: иначе записали
            # бы «пустое» состояние и сломали рабочую сессию HTTP-цикла.
            try:
                if not _on_signin(page, cfg):
                    await _store_session(context, account)
            except Exception:
                pass
            await browser.close()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m app.take_capture",
        description=(
            "Живой (headful) браузер: автообновление страницы очереди, клик по кнопке "
            "взятия и захват реального запроса/ответа для перехода на HTTP-режим."
        ),
    )
    parser.add_argument("--account", type=int, default=1, help="номер кабинета (1-based)")
    parser.add_argument(
        "--refresh", type=float, default=3.0, help="интервал автообновления страницы, сек"
    )
    parser.add_argument(
        "--click-wait", type=float, default=2.5, help="пауза после клика для сбора ответа, сек"
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="общий лимит работы, сек")
    parser.add_argument(
        "--login-wait", type=float, default=300.0, help="сколько ждать ручного входа, сек"
    )
    parser.add_argument(
        "--max-takes", type=int, default=1, help="сколько заявок взять (0 — без ограничения)"
    )
    parser.add_argument("--button-text", default="", help="доп. тексты кнопки через запятую")
    parser.add_argument(
        "--out", default="", help="каталог для дампов (по умолчанию ./data/take_capture)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="без окна (по умолчанию окно видно)"
    )
    parser.add_argument(
        "--no-click", action="store_true", help="только обновлять страницу, не кликать"
    )
    parser.add_argument(
        "--close", action="store_true", help="закрыть браузер сразу после работы"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    from dotenv import load_dotenv

    load_dotenv()  # локально читает .env; в Docker переменные уже в окружении
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    opts = _parse_args(argv)
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"❌ Не удалось прочитать конфиг: {exc}")
        return 1
    try:
        account = cfg.accounts[opts.account - 1]
    except IndexError:
        print(f"❌ Кабинет #{opts.account} не задан (всего {len(cfg.accounts)}).")
        return 1

    print(f"👤 Кабинет #{account.index} «{account.name}», сессия: {account.storage_state_path}")
    print("ℹ️ Сначала остановите бота в Telegram (/stop), иначе будете конкурировать сами с собой.")
    try:
        return asyncio.run(run_capture(cfg, account, opts))
    except KeyboardInterrupt:
        print("\n⏹ Остановлено пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
