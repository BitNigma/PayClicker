"""Горячий цикл опроса и взятия заявок на ЧИСТОМ HTTP (httpx), без браузера.

Зачем: browser-based polling (Playwright + reload страницы целиком) тратит
десятки-сотни миллисекунд на сетевой круг браузера, парсинг DOM, выполнение
JS и ожидание load-состояния. Здесь тот же сценарий выполняется обычными
HTTP-запросами: GET очереди -> мгновенный разбор HTML регулярками -> POST
на "взять заявку". Браузер используется ТОЛЬКО для разовой авторизации
(получение cookies в auth.json), в горячем цикле его нет.

Каждое взятие инструментировано: в ./data/metrics.jsonl (JSONL) и в лог
пишется замер saw_to_sent_ms — сколько прошло между "увидели заявку в ответе
GET" и "отправили POST". Это и есть фактическое время реакции Python-клиента;
по нему видно, где реально теряются миллисекунды: в сети или в накладных
расходах клиента (тогда и стоит думать про Go).

Размер активной очереди считается по площадке, а не по памяти: перед стартом и
затем раз в DEMANDS_SYNC_S секунд бот сверяется со страницей demands (сколько
заявок реально «в работе»). Причины две: очередь в памяти живёт только до
перезапуска процесса, а часть заявок площадка выдаёт автоматически (без нашего
«Забрать выплату») — обе занимают слот и должны учитываться в цель TARGET_QUEUE_SIZE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import httpx

from .config import Config
from .state import WorkerState

logger = logging.getLogger("http_worker")

SITE_HOST = "spark-trading.cash"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

EMPTY_ROW_TEXT = "не найдено"
STATUS_IN_WORK_MARKER = "в работе у провайдера"


class SessionExpired(Exception):
    """Сессия (cookies) больше не действительна — нужно обновить auth.json."""


class BlockedError(Exception):
    """Антибот (DataDome/Cloudflare) отдал challenge/403 вместо обычного HTML."""


# Сколько раз подряд разрешено сбросить cookies и авторизоваться заново после
# блокировки (403/антибот). Ограничение, чтобы не зациклиться на устойчивом
# блоке: если помогает — счётчик сбрасывается при первой же рабочей сессии.
MAX_BLOCK_REAUTH = max(1, int(os.environ.get("BLOCK_REAUTH_ATTEMPTS", "3")))

# Как часто максимум слать в Telegram подтверждение, что превентивные POST уходят
# (иначе окна каждые PREDICTIVE_TAKE_GAP_MS превратились бы в спам). Секунды.
PREDICTIVE_NOTIFY_MIN_S = 3.0

# Минимальная пауза между проходами предсказателя, если PREDICTIVE_TAKE_GAP_MS=0.
# Нужна только чтобы не крутиться вхолостую, когда окно целиком отфильтровано.
PREDICTIVE_RETRY_MIN_S = 0.05

# Сколько символов тела ответа сохранять в дампе take_request_*.json. Первых 4000
# мало: там только <head> — тело с таблицей/сообщением не видно. 20000 хватает,
# чтобы разобрать разметку и подобрать TAKE_SUCCESS_MARKERS / TAKE_GONE_MARKERS.
TAKE_DUMP_BODY_CHARS = 20000


# ------------------------- разбор HTML (регулярки) -------------------------
_TR_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_PAYOUT_ID_RE = re.compile(r"name=[\"']payout_id[\"']\s+value=[\"'](\d+)[\"']", re.I)
_CSRF_RE = re.compile(r"name=[\"']csrf_token[\"']\s+value=[\"']([^\"']+)[\"']", re.I)
_ACCESS_KEY_RE = re.compile(r"name=[\"']access_key[\"']\s+value=[\"']([^\"']*)[\"']", re.I)
_ROW_ID_TEXT_RE = re.compile(r"#\s*(\d+)")
_BADGE_RE = re.compile(
    r"<span[^>]*class=[\"'][^\"']*badge[^\"']*[\"'][^>]*>(.*?)</span>", re.S | re.I
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(value: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()


def parse_rows(html: str) -> list[dict]:
    """Строки заявок из HTML страницы выплат (очередь ИЛИ demands).

    Возвращает список dict: id, status, csrf_token, access_key.
    Строка-заглушка «Не найдено» и строки без признаков заявки пропускаются.
    Одна и та же функция подходит и для очереди (есть форма take_payout),
    и для demands (там id берётся из текста «# 6408416»).
    """
    rows: list[dict] = []
    for match in _TR_RE.finditer(html):
        block = match.group(0)
        # берём только строки таблицы заявок, а не любые другие <tr> на странице
        if "payout-status" not in block.lower() and not _PAYOUT_ID_RE.search(block):
            continue
        text = _strip_tags(block)
        if not text or EMPTY_ROW_TEXT in text.lower():
            continue

        payout_id = _PAYOUT_ID_RE.search(block)
        if payout_id:
            order_id = payout_id.group(1)
        else:
            fallback = _ROW_ID_TEXT_RE.search(text)
            order_id = fallback.group(1) if fallback else None
        if not order_id:
            continue

        badge = _BADGE_RE.search(block)
        csrf = _CSRF_RE.search(block)
        access_key = _ACCESS_KEY_RE.search(block)
        rows.append(
            {
                "id": order_id,
                "status": _strip_tags(badge.group(1)) if badge else "",
                "csrf_token": csrf.group(1) if csrf else None,
                "access_key": access_key.group(1) if access_key else "",
            }
        )
    return rows


def _ids_text(ids) -> str:
    """«#6408416, #6408417» — для уведомлений в Telegram."""
    return ", ".join(f"#{i}" for i in sorted(ids))


# ------------------- разбор тела ответа на POST take_payout -------------------
# Значения вердикта _classify_take_response().
TAKE_REPLY_TAKEN = "taken"                    # сервер подтвердил: заявка наша
TAKE_REPLY_GONE = "gone"                      # заявки нет / её забрали раньше
TAKE_REPLY_STILL_AVAILABLE = "still_available"  # заявка ещё в очереди (не взяли)
TAKE_REPLY_UNKNOWN = "unknown"                # по ответу понять нельзя

# Человекочитаемые пояснения для Telegram/логов.
_TAKE_REPLY_TEXT = {
    TAKE_REPLY_TAKEN: "сервер подтвердил взятие",
    TAKE_REPLY_GONE: "заявки уже нет (её забрали раньше нас)",
    TAKE_REPLY_STILL_AVAILABLE: "заявка всё ещё в очереди (не успели забрать)",
    TAKE_REPLY_UNKNOWN: "ответ сервера не распознан",
}


def _take_form_block(html: str, order_id: str) -> str:
    """Строка таблицы очереди ровно с этим payout_id (или пустая строка)."""
    needle = re.compile(
        r"name=[\"']payout_id[\"']\s+value=[\"']" + re.escape(order_id) + r"[\"']", re.I
    )
    for match in _TR_RE.finditer(html or ""):
        if needle.search(match.group(0)):
            return match.group(0)
    return ""


def _classify_take_response(html: str, order_id: str, cfg: Config) -> str:
    """Пытается понять по ТЕЛУ ответа на POST take_payout, что произошло.

    Возвращает одну из констант TAKE_REPLY_*. Это ДИАГНОСТИКА/пояснение к
    уведомлению: окончательное решение о том, наша ли заявка, по-прежнему даёт
    сверка со страницей demands (``_confirm_taken``), потому что ответ на POST
    площадка подменяет без надёжного признака успеха.

    Порядок проверок:
      1) маркеры «заявки нет» из cfg.take_gone_markers (если заданы в env);
      2) маркеры успеха из cfg.take_success_markers (если заданы в env);
      3) структурно: в ответе есть строка этого заявки со статусом
         «в работе у провайдера» → взяли;
      4) структурно: в ответе всё ещё есть форма «Забрать выплату» для этого id
         → заявка не наша (ещё в очереди).
    Иначе — «неизвестно».
    """
    low = (html or "").lower()
    if any(m in low for m in getattr(cfg, "take_gone_markers", ())):
        return TAKE_REPLY_GONE
    if any(m in low for m in getattr(cfg, "take_success_markers", ())):
        return TAKE_REPLY_TAKEN
    block = _take_form_block(html or "", order_id)
    if block:
        # Строка всё ещё предлагает «Забрать выплату» — проверяем её статус:
        if STATUS_IN_WORK_MARKER in block.lower():
            return TAKE_REPLY_TAKEN
        return TAKE_REPLY_STILL_AVAILABLE
    # Нашей строки в ответе нет. Если это ВООБЩЕ страница очереди (есть таблица
    # выплат), значит нашей заявки в очереди больше нет: её забрали (мы/другие)
    # или её и не было (для предсказанного id). Неизвестно — только если страница
    # вообще не похожа на очередь (например, редирект на дашборд).
    page_markers = ("payout-table", "payout-status", "payout-card")
    if any(m in low for m in page_markers):
        return TAKE_REPLY_GONE
    return TAKE_REPLY_UNKNOWN


def in_work_ids(rows: list[dict]) -> set[str]:
    """id заявок, которые занимают слот целевой очереди (статус «в работе у провайдера»).

    На странице demands это и заявки, взятые нами, и те, что площадка выдала
    автоматически (без нажатия «Забрать выплату»): место в очереди занимают обе
    категории, поэтому считаем их одинаково. Завершённые и ошибочные заявки сюда
    не попадают — их слот свободен.
    """
    return {
        r["id"]
        for r in rows
        if r.get("id") and STATUS_IN_WORK_MARKER in (r.get("status") or "").lower()
    }


def load_cookies(account) -> dict:
    """Cookies сайта из storage_state (auth.json кабинета). Берём только домен spark-trading.cash.

    Формат auth.json — Playwright storage_state: {"cookies": [...], "origins": [...]}.
    """
    path = Path(account.storage_state_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Не удалось прочитать %s", path)
        return {}
    cookies: dict = {}
    for cookie in data.get("cookies", []):
        domain = str(cookie.get("domain", "")).lstrip(".")
        if not domain:
            continue
        if SITE_HOST == domain or SITE_HOST.endswith("." + domain):
            cookies[cookie["name"]] = cookie["value"]
    return cookies


# Заглушки антибота/блокировки: DataDome-подобные защиты и Cloudflare, когда он
# отдаёт challenge/403 «Just a moment…» вместо страницы. Без этих маркеров
# такая заглушка выглядела бы как обычная страница ("ok"), и горячий цикл молча
# крутился бы вхолостую (при этом сам ответ сайта — 403 Forbidden).
_BLOCKED_MARKERS = (
    "captcha-delivery.com",
    "datadome",
    "geo.captcha",
    "cf_chl_opt",
    "challenge-platform",
    "challenges.cloudflare.com",
    "just a moment",
    "verify you are human",
    "enable javascript and cookies",
)


def _reset_stored_session(account) -> bool:
    """Сбрасывает сохранённые cookies кабинета (удаляет auth.json).

    Нужно, когда сайт блокирует уже сохранённую сессию (403/Cloudflare): старый
    cf_clearance мог «сгореть», и авторизацию надо пройти с нуля. Возвращает True,
    если файл сессии реально был и удалён.
    """
    path = Path(account.storage_state_path)
    try:
        if path.exists():
            path.unlink()
            logger.info("Сброшена сохранённая сессия кабинета: %s", path)
            return True
    except Exception:
        logger.exception("Не удалось сбросить сохранённую сессию %s", path)
    return False


def _classify(html: str, url: str) -> str:
    """ok | signin | blocked — по содержимому ответа и итоговому URL."""
    low = html.lower()
    if any(m in low for m in _BLOCKED_MARKERS):
        return "blocked"
    if "/signin" in url or "auth-form" in low:
        return "signin"
    return "ok"


def _default_headers(cfg: Config) -> dict:
    return {
        "user-agent": os.environ.get("HTTP_USER_AGENT", DEFAULT_USER_AGENT),
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "upgrade-insecure-requests": "1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
    }


class Metrics:
    """Пишет замеры времени в JSONL-файл и в лог.

    Ключевой замер — saw_to_sent_ms: от момента, когда заявка уже видна в
    полученном ответе GET, до момента отправки POST. Сопоставив его с
    poll_rtt_ms/submit_rtt_ms (сеть) и py_overhead_ms (чистый Python),
    можно понять, где узкое место.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = Path(cfg.metrics_path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Не удалось создать каталог для метрик")

    def record(self, **fields) -> None:
        fields.setdefault("ts", round(time.time(), 3))
        line = json.dumps(fields, ensure_ascii=False)
        logger.info("METRIC %s", line)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            logger.exception("Не удалось записать метрику в %s", self.path)


class PayoutClient:
    """Async HTTP-клиент сайта. Держит одно keep-alive соединение и cookies сессии."""

    def __init__(self, cfg: Config, cookies: dict):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=f"https://{SITE_HOST}",
            cookies=cookies,
            headers=_default_headers(cfg),
            timeout=httpx.Timeout(cfg.http_timeout_s),
            follow_redirects=True,
            http2=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check(self, response: httpx.Response) -> None:
        kind = _classify(response.text, str(response.url))
        # 403/429 — сайт (Cloudflare/антибот) заблокировал HTTP-клиент: даже если
        # тело не похоже на challenge, продолжать опрос бессмысленно.
        if kind == "blocked" or response.status_code in (403, 429):
            raise BlockedError(
                f"Сайт отдал антибот-заглушку/HTTP {response.status_code} вместо "
                "страницы: HTTP-клиент распознан (DataDome/Cloudflare). Нужно "
                "обновить cookies через браузерную авторизацию (или вернуться на "
                "WORKER_BACKEND=browser)."
            )
        if kind == "signin":
            raise SessionExpired("Сессия недействительна — сайт редиректит на /signin.")

    async def fetch_queue(self) -> str:
        response = await self._client.get(self.cfg.queue_url)
        self._check(response)
        return response.text

    async def fetch_demands(self) -> str:
        response = await self._client.get(self.cfg.demands_url)
        self._check(response)
        return response.text

    async def take_payout(
        self, order_id: str, csrf_token: str, access_key: str = ""
    ) -> httpx.Response:
        """POST ровно той формы, что и кнопка «Забрать выплату» в браузере."""
        data = {
            "csrf_token": csrf_token,
            "act": "take_payout",
            "payout_id": order_id,
            "access_key": access_key,
        }
        headers = {
            "referer": self.cfg.queue_url,
            "origin": f"https://{SITE_HOST}",
            "content-type": "application/x-www-form-urlencoded",
        }
        return await self._client.post(self.cfg.queue_url, data=data, headers=headers)


def _queue_snapshot_text(cfg: Config, ws: WorkerState) -> str:
    """Снимок занятых слотов. Источник — площадка (ws.demands_ids), а не только память:
    после рестарта процесса взятые ранее заявки тоже занимают слот."""
    ids = sorted(ws.demands_ids | ws.taken_order_ids)
    if not ids:
        return f"Очередь пуста (0/{cfg.target_queue_size})."
    return (
        f"В работе сейчас ({len(ids)}/{cfg.target_queue_size}): "
        + _ids_text(ids)
    )


def _sleep_ms(lo: int, hi: int) -> float:
    return random.uniform(lo, hi) / 1000


def _poll_wait(cfg: Config) -> float:
    """Пауза между GET очереди в _poll_loop (секунды).

    По умолчанию (`predictive_take_poll_ms = 0`) — АКТИВНЫЙ горячий цикл:
    обычный `TAKE_POLL_MIN/MAX_MS`. Предиктив лишь ДОБАВЛЯЕТ спекулятивные POST,
    а не заменяет опрос: именно активный GET ловит реальные заявки и обновляет
    якорь/CSRF. Если явно задан `PREDICTIVE_TAKE_POLL_MS > 0` — опрос становится
    редким (джиттер до +50%), чтобы снизить GET-нагрузку/риск Cloudflare.
    """
    base = getattr(cfg, "predictive_take_poll_ms", 0) or 0
    if cfg.predictive_take and base > 0:
        return _sleep_ms(base, int(base * 1.5))
    return _sleep_ms(cfg.take_poll_min_ms, cfg.take_poll_max_ms)


def _effective_pollers(cfg: Config) -> int:
    """Сколько воронок опроса реально запускать.

    По умолчанию — все `TAKE_PARALLEL_POLLERS`: горячий GET-цикл работает на полную
    (предиктив лишь ДОБАВЛЯЕТ POST, не заменяя опрос).

    Воронки сводим к одной ТОЛЬКО в «тихом» режиме, т.е. когда заданный
    `PREDICTIVE_TAKE_POLL_MS` ЯВНО больше обычного интервала (`TAKE_POLL_MAX_MS`).
    Если же он меньше/равен (например, 1 мс — «максимально быстро»), все воронки
    остаются: иначе настройка «ускорить» парадоксально уменьшала бы их число.
    """
    pollers = max(1, cfg.take_parallel_pollers)
    base = getattr(cfg, "predictive_take_poll_ms", 0) or 0
    if cfg.predictive_take and pollers > 1 and base > cfg.take_poll_max_ms:
        return 1
    return pollers


async def _session_ok(client: PayoutClient) -> bool:
    try:
        await client.fetch_queue()
        return True
    except SessionExpired:
        return False
    except BlockedError:
        raise
    except Exception:
        logger.exception("Проверка сессии не удалась (сеть?)")
        return False


async def ensure_session(
    cfg: Config, account, ws: WorkerState, notify, notify_file=None, force_reauth: bool = False
) -> dict | None:
    """Возвращает рабочие cookies кабинета. Если сохранённые не годятся — разово
    поднимает браузер, чтобы обновить auth.json кабинета (вне горячего цикла).
    notify_file пробрасывается в браузерную авторизацию, чтобы HTML-дампы
    проблемных страниц уходили прямо в Telegram.

    force_reauth=True — не проверять и не использовать сохранённые cookies, а
    сразу пройти авторизацию с нуля (используется при блокировке 403/антибот,
    когда старый cf_clearance «сгорел»).
    """
    cookies = {} if force_reauth else load_cookies(account)
    if cookies:
        probe = PayoutClient(cfg, cookies)
        try:
            if await _session_ok(probe):
                return cookies
        finally:
            await probe.aclose()

    await notify("🔐 Cookies недействительны/сброшены — разовая браузерная авторизация (вне горячего цикла)...")
    from .browser_worker import login_via_browser  # локальный импорт: браузер нужен только тут

    ok = await login_via_browser(cfg, account, ws, notify, notify_file)
    if not ok:
        return None
    return load_cookies(account) or None


def _safe_text(response: httpx.Response) -> str:
    """Тело ответа как текст, НЕ падая на битой/неизвестной кодировке.

    `response.text` декодирует через заявленный charset и может бросить
    UnicodeDecodeError. Разбор ответа — вспомогательный, поэтому любые проблемы с
    кодировкой не должны ронять такт взятия: тогда возвращаем «грубое» декодирование.
    """
    try:
        return response.text or ""
    except Exception:
        try:
            return response.content.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _dump_take_request(
    cfg: Config,
    order_id: str,
    data: dict,
    response: httpx.Response,
    server_reply: str | None = None,
    predicted: bool = False,
) -> Path:
    """Сохраняет фактический HTTP POST и ответ — для сверки с версией на браузере.

    В дамп пишем и тело ответа (усечённое) и вердикт `_classify_take_response`:
    это единственный способ ВРУЧНУЮ сравнить, чем ответ сайта отличается, когда
    заявку реально взяли, от случая «такой заявки ещё нет / её забрали раньше».
    Сам бот успех определяет не по этому ответу, а сверкой со страницей demands
    (см. _confirm_taken); здесь — только диагностика.

    predicted=True — дамп предсказательной отправки: в имени файла добавляем
    `_pred`, чтобы их было видно отдельно (пишутся только при PREDICTIVE_TAKE_DUMPS).
    """
    d = Path(cfg.log_path).parent
    d.mkdir(parents=True, exist_ok=True)
    suffix = "_pred" if predicted else ""
    fp = d / f"take_request_http_{order_id}_{int(time.time())}{suffix}.json"
    body = _safe_text(response)
    payload = {
        "transport": "httpx",
        "predicted": predicted,
        "request": {
            "url": cfg.queue_url,
            "method": "POST",
            "headers": {"content-type": "application/x-www-form-urlencoded"},
            "post_data": "&".join(f"{k}={v}" for k, v in data.items()),
        },
        "response": {
            "url": str(response.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "text_head": body[:TAKE_DUMP_BODY_CHARS],
            "server_reply": server_reply,
        },
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


async def _confirm_taken(
    client: PayoutClient, cfg: Config, order_id: str, attempts: int = 4, delay: float = 0.4
) -> bool:
    """Быстрая проверка, что заявка реально появилась в нашей очереди на demands."""
    for _ in range(attempts):
        try:
            html = await client.fetch_demands()
            if any(r["id"] == order_id for r in parse_rows(html)):
                return True
        except SessionExpired:
            raise
        except Exception:
            logger.exception("Ошибка при подтверждении взятия заявки #%s", order_id)
        await asyncio.sleep(delay)
    return False


async def fetch_demand_rows(client: PayoutClient, ws: WorkerState) -> list[dict] | None:
    """Строки страницы demands (все статусы) или None, если страницу прочитать не удалось.

    Единственный запрос, который говорит правду о занятых слотах: там и наши
    заявки, и выданные площадкой автоматически, и оставшиеся с прошлого запуска
    процесса. SessionExpired пробрасывается наверх — это признак, что сессия
    кабинета слетела.
    """
    try:
        html = await client.fetch_demands()
    except SessionExpired:
        raise
    except Exception as exc:
        logger.warning("[%s] сверка со страницей demands не удалась: %s", ws.name, exc)
        return None
    return parse_rows(html)


async def _do_take(
    client: PayoutClient,
    cfg: Config,
    account,
    ws: WorkerState,
    notify,
    metrics: Metrics,
    row: dict,
    timing: dict,
    t_recv: float,
    inflight: set,
    predicted: bool = False,
) -> bool:
    """Один такт по заявке: POST -> метрика -> подтверждение -> результат.

    Возвращает True, если заявку удалось подтвердить (она наша).

    Запускается отдельной задачей, чтобы НЕ блокировать следующий GET очереди:
    сетевой RTT отправки POST перекрывается следующим опросом (это убирает
    ~150-270 мс «слепоты» на каждое взятие).

    predicted=True — это превентивное взятие «будущего» id (его ещё не было в HTML).
    Для таких заявок не пишем дампы и не шумим «не подтвердилось»: неудача —
    нормальный исход, если заявки с таким id пока просто нет.
    """
    order_id = row["id"]
    csrf_token = row.get("csrf_token")
    access_key = row.get("access_key", "")
    # Предсказанные id НЕ повторяем (их много; повтор = POST-шторм). Для реальных
    # взятий повторяем POST, если ответ неоднозначен: заявка ещё в очереди
    # (still_available) или ответ не распознан (unknown). taken/gone не повторяем.
    retry_limit = 0 if predicted else max(0, cfg.take_retry_count)
    retry_delay = max(0, cfg.take_retry_delay_ms) / 1000
    server_reply = TAKE_REPLY_UNKNOWN
    try:
        for attempt in range(1, retry_limit + 2):
            t_post = time.perf_counter()
            try:
                response = await client.take_payout(order_id, csrf_token, access_key)
            except Exception as exc:
                logger.warning("[%s] POST take_payout для #%s не удался: %s", account.name, order_id, exc)
                return False
            t_post_done = time.perf_counter()

            # Разбор ТЕЛА ответа сервера: помогает понять/озвучить, что произошло
            # (взяли / заявки нет / ещё в очереди). Это не замена подтверждению через
            # demands, а пояснение к нему — см. _classify_take_response.
            server_reply = _classify_take_response(_safe_text(response), order_id, cfg)

            saw_to_sent = round((t_post - t_recv) * 1000, 2)
            metrics.record(
                event="take",
                account=account.name,
                account_index=account.index,
                order_id=order_id,
                poll_rtt_ms=timing.get("poll_rtt_ms"),
                parse_ms=timing.get("parse_ms"),
                since_prev_get_ms=timing.get("since_prev_get_ms"),
                saw_to_sent_ms=saw_to_sent,
                py_overhead_ms=round(saw_to_sent - (timing.get("parse_ms") or 0.0), 2),
                submit_rtt_ms=round((t_post_done - t_post) * 1000, 2),
                http_status=response.status_code,
                server_reply=server_reply,
                predicted=predicted,
                attempt=attempt,
            )
            if cfg.debug:
                logger.info(
                    "[%s] ответ сервера на POST #%s (попытка %d/%d): %s (%s)%s",
                    account.name, order_id, attempt, retry_limit + 1, server_reply,
                    _TAKE_REPLY_TEXT.get(server_reply, ""),
                    " [предсказание]" if predicted else "",
                )

            # Дамп POST-запроса — только локально (./data), в чат НЕ отправляем.
            # Пишем один дамп на такт (первая попытка): имя файла содержит только
            # секунду, повторы затёрли бы его. Предсказанные — при PREDICTIVE_TAKE_DUMPS.
            if cfg.debug_capture_attempts and attempt == 1 and (
                not predicted or cfg.predictive_take_dumps
            ):
                try:
                    data = {
                        "csrf_token": csrf_token,
                        "act": "take_payout",
                        "payout_id": order_id,
                        "access_key": access_key,
                    }
                    _dump_take_request(cfg, order_id, data, response, server_reply, predicted)
                except Exception:
                    logger.exception("Не удалось сохранить дамп запроса для #%s", order_id)

            t_confirm = time.perf_counter()
            try:
                if predicted:
                    # Предсказанные id подтверждаем дешевле: большинство из них ещё
                    # не существуют, а лишние GET на limits-страницу приближают блок.
                    confirmed = await _confirm_taken(client, cfg, order_id, attempts=2, delay=0.25)
                elif server_reply == TAKE_REPLY_STILL_AVAILABLE:
                    # Заведомо «не взяли» (форма ещё на странице): не ждём 1.6 с,
                    # одна быстрая проверка — чтобы сразу перейти к повтору POST.
                    confirmed = await _confirm_taken(client, cfg, order_id, attempts=1, delay=0.0)
                else:
                    confirmed = await _confirm_taken(client, cfg, order_id)
            except SessionExpired:
                confirmed = False
            metrics.record(
                event="take_result",
                account=account.name,
                account_index=account.index,
                order_id=order_id,
                confirmed=confirmed,
                server_reply=server_reply,
                predicted=predicted,
                attempt=attempt,
                confirm_ms=round((time.perf_counter() - t_confirm) * 1000, 2),
            )
            if confirmed:
                ws.taken_count += 1
                ws.taken_order_ids.add(order_id)
                # Заявка сразу занимает слот: не ждём следующей сверки с demands.
                ws.demands_ids.add(order_id)
                if predicted:
                    await notify(f"🎯 Поймал предсказанную заявку #{order_id} (HTTP)")
                else:
                    await notify(f"✅ Взята заявка #{order_id} (HTTP)")
                if cfg.debug:
                    await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg, ws)}")
                return True

            # Не подтвердилось. При неоднозначном ответе (заявка ещё в очереди или
            # ответ не распознан) повторяем POST ограниченно: возможно, проиграли
            # гонку на волоске и со следующей попытки возьмём. taken/gone — не повторяем.
            if attempt <= retry_limit and server_reply in (
                TAKE_REPLY_STILL_AVAILABLE, TAKE_REPLY_UNKNOWN
            ):
                if cfg.debug:
                    logger.info(
                        "[%s] #%s не подтверждено (%s) — повтор POST, попытка %d/%d",
                        account.name, order_id, server_reply, attempt + 1, retry_limit + 1,
                    )
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                continue
            break

        # Не взяли: попытки исчерпаны или повтор не нужен. Сообщаем один раз.
        if not predicted:
            # «нет такой заявки» — нормальная гонка (её забрали раньше нас), а
            # «ещё в очереди» означает, что наш POST не сработал — стоит присмотреться.
            reason = _TAKE_REPLY_TEXT.get(server_reply, "")
            if server_reply == TAKE_REPLY_STILL_AVAILABLE:
                await notify(
                    f"⚠️ Заявку #{order_id} сервер не отдал: {reason}. "
                    "Проверьте форму «Забрать выплату» на сайте."
                )
            else:
                await notify(
                    f"ℹ️ Заявку #{order_id} не взяли: {reason or 'похоже, её забрали раньше нас'}."
                )
        return False
    finally:
        inflight.discard(order_id)


async def demands_loop(client: PayoutClient, cfg: Config, ws: WorkerState, notify) -> None:
    """Раз в DEMANDS_SYNC_S секунд сверяемся со страницей demands: сколько заявок в работе.

    Это единственный источник правды о занятых слотах, потому что в памяти бота
    заявок может не быть, хотя место в очереди они занимают:
      • заявку взяли мы (тогда она есть и в ws.taken_order_ids);
      • заявку площадка выдала автоматически — в памяти её не было вовсе;
      • заявка осталась в работе с прошлого запуска процесса (память сбрасывается);
      • заявка завершилась (статус уже не «в работе у провайдера») — слот свободен.

    Раньше цикл смотрел только на ws.taken_order_ids и потому не видел ни
    автовыдачи, ни заявок, взятых до перезапуска.
    """
    while ws.running and ws.mode == "take":
        try:
            rows = await fetch_demand_rows(client, ws)
        except SessionExpired:
            return
        if rows is not None:
            statuses = {r["id"]: r.get("status", "") for r in rows if r.get("id")}
            in_work = in_work_ids(rows)
            stale = ws.demands_ids
            ws.demands_ids = in_work
            # Заявки, которые были в работе, а теперь нет — их слот освободился.
            for oid in sorted(stale - in_work):
                if oid in ws.taken_order_ids:
                    ws.taken_order_ids.discard(oid)
                    status = statuses.get(oid) or "завершена"
                    await notify(
                        f"ℹ️ Заявка #{oid}: статус «{status}» — убираю из очереди, слот свободен."
                    )
                    if cfg.debug:
                        await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg, ws)}")
            # Заявки, которых мы не брали (автовыдача площадки) — тоже занимают слот.
            auto = sorted(in_work - stale - ws.taken_order_ids)
            if auto:
                await notify(
                    f"📦 Автовыдача: {_ids_text(auto)} уже в работе "
                    f"(площадка выдала без нашего взятия)."
                )
                if cfg.debug:
                    await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg, ws)}")
        for _ in range(max(1, cfg.demands_sync_s)):
            if not (ws.running and ws.mode == "take"):
                break
            await asyncio.sleep(1)


async def watch_loop(
    client: PayoutClient, cfg: Config, account, ws: WorkerState, notify, notify_file, metrics: Metrics
) -> None:
    """Режим наблюдения по HTTP: ничего не берём, только уведомляем о новых заявках."""
    await notify("👀 HTTP-наблюдение запущено (без браузера). Сообщу, как только появится заявка.")
    # Показываем реальную картину с площадки: сколько заявок уже в работе (в памяти
    # их может не быть — часть выдаёт автовыдача, часть осталась с прошлого запуска).
    try:
        rows = await fetch_demand_rows(client, ws)
    except SessionExpired:
        ws.logged_in = False
        ws.running = False
        await notify("⚠️ Сессия слетела. Останавливаю наблюдение. Запусти кабинет заново (/run).")
        return
    if rows is not None:
        ws.demands_ids = in_work_ids(rows)
        ws.taken_order_ids.intersection_update(ws.demands_ids)
        if cfg.debug:
            await notify(
                f"📊 Сверка с площадкой: в работе уже "
                f"{len(ws.demands_ids)}/{cfg.target_queue_size} заявок "
                f"(цель задаётся TARGET_QUEUE_SIZE)."
            )
    prev_ids: set = set()

    while ws.running and ws.mode == "watch":
        if ws.resync_required:
            # /reset_session во время наблюдения: очищаем кэш и делаем свежую сверку.
            await resync_demands_loop(client, cfg, ws, notify)
            continue
        t_sent = time.perf_counter()
        try:
            html = await client.fetch_queue()
        except SessionExpired:
            ws.logged_in = False
            ws.running = False
            await notify("⚠️ Сессия слетела. Останавливаю наблюдение. Запусти кабинет заново (/run).")
            break
        except BlockedError as exc:
            # Блокировка (403/антибот, Cloudflare): останавливаем наблюдение и
            # просим run_worker сбросить cookies и переавторизоваться (needs_reauth).
            ws.needs_reauth = True
            ws.running = False
            await notify(f"⛔ {exc}")
            break
        except Exception as exc:
            logger.warning("[%s] watch GET failed: %s", account.name, exc)
            await asyncio.sleep(1)
            continue
        t_recv = time.perf_counter()

        rows = parse_rows(html)
        t_parsed = time.perf_counter()
        ids = {r["id"] for r in rows}
        new_ids = ids - prev_ids

        if new_ids:
            ts = int(time.time())
            d = Path(cfg.log_path).parent
            d.mkdir(parents=True, exist_ok=True)
            fp = d / f"order_http_{account.index}_{ts}_page.html"
            fp.write_text(html, encoding="utf-8")
            preview = "\n".join(f"• #{r['id']} {r['status']}" for r in rows[:3])
            await notify(f"🔔 Появилась заявка (HTTP)! Сейчас на странице: {len(rows)}\n{preview}")
            await notify_file(str(fp), "HTML очереди (HTTP, без браузера)")
            metrics.record(
                event="seen",
                account=account.name,
                account_index=account.index,
                mode="watch",
                count=len(rows),
                new_ids=sorted(new_ids),
                poll_rtt_ms=round((t_recv - t_sent) * 1000, 2),
                parse_ms=round((t_parsed - t_recv) * 1000, 2),
            )
        prev_ids = ids
        ws.last_status = f"watch(http): заявок на странице={len(rows)}"
        await asyncio.sleep(_sleep_ms(cfg.reload_min_ms, cfg.reload_max_ms))


def _observe_queue(ws: WorkerState, rows: list[dict]) -> None:
    """Запоминает из HTML очереди общий csrf_token и «якорь» — максимальный id.

    При первом наблюдении курсор предсказателя (ws.last_order_id) ставим в этот
    максимальный id; дальше его двигает predict_loop. ws.observed_anchor — максимум
    реально увиденных id (по нему ограничиваем забегание вперёд).
    """
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    if not ids:
        return
    csrf = next((r.get("csrf_token") for r in rows if r.get("csrf_token")), None)
    if csrf:
        ws.last_csrf_token = csrf
    top = max(ids)
    if top > ws.observed_anchor:
        ws.observed_anchor = top
        ws.predictive_rounds = 0
        # «Обнуляем отсчёт пула» от реально увиденной заявки: следующее окно POST
        # пойдёт на top+1…, а не от «убежавшего» вперёд курсора. Чистим и набор
        # уже отправленных, чтобы окно от top снова было полным.
        ws.last_order_id = top
        ws.predictive_sent.clear()
    elif ws.last_order_id == 0:
        ws.last_order_id = top


def _fire_predicted_window(
    client: PayoutClient,
    cfg: Config,
    account,
    ws: WorkerState,
    notify,
    metrics: Metrics,
    inflight: set,
    take_tasks: set,
) -> list:
    """Шлёт ОДНО окно предсказаний: ws.last_order_id+1 … ws.last_order_id+N.

    Курсор сдвигается на наибольший id окна, поэтому следующее окно начнётся сразу
    за ним — это и есть «взять самый большой id и продолжить с него». Возвращает
    список запущенных задач, чтобы можно было дождаться их результата.
    """
    csrf = ws.last_csrf_token
    if not csrf or ws.last_order_id <= 0:
        return []
    start = ws.last_order_id
    tasks = []
    fired: list[str] = []
    for n in range(1, cfg.predictive_take_count + 1):
        pid = str(start + n)
        if (
            pid in inflight
            or pid in ws.demands_ids
            or pid in ws.taken_order_ids
            or pid in ws.predictive_sent
        ):
            continue
        ws.predictive_sent.add(pid)
        # Спекулятивная отправка: помечаем отдельно от реального inflight, чтобы
        # она НЕ считалась занятым слотом и не замораживала опрос очереди.
        ws.predicted_inflight.add(pid)
        inflight.add(pid)
        row = {"id": pid, "csrf_token": csrf, "access_key": ""}
        task = asyncio.create_task(
            _do_take(
                client, cfg, account, ws, notify, metrics, row, {}, time.perf_counter(),
                inflight, predicted=True,
            )
        )
        take_tasks.add(task)
        task.add_done_callback(take_tasks.discard)
        task.add_done_callback(lambda _t, _pid=pid: ws.predicted_inflight.discard(_pid))
        tasks.append(task)
        fired.append(pid)
    # Всегда логируем окно предсказаний — иначе кажется, что «превентивных
    # POST нет» (они не дампятся и не шумят уведомлениями по умолчанию).
    if fired:
        logger.info(
            "[%s] 🔮 превентивно: окно #%s…#%s — %d POST take_payout (predicted)",
            ws.name, fired[0], fired[-1], len(fired),
        )
    ws.last_order_id = start + cfg.predictive_take_count
    # Подчищаем набор уже отправленных id: курсор идёт только вперёд, поэтому
    # всё, что не больше старого курсора, больше не актуально (не пухнем в памяти).
    ws.predictive_sent = {p for p in ws.predictive_sent if p.isdigit() and int(p) > start}
    return tasks


async def predict_loop(
    client: PayoutClient,
    cfg: Config,
    account,
    ws: WorkerState,
    notify,
    metrics: Metrics,
    inflight: set,
    take_tasks: set,
) -> None:
    """Превентивный поиск будущих заявок окнами по PREDICTIVE_TAKE_COUNT.

    Пока есть csrf_token и курсор: шлём окно take_payout на следующие N id. Если
    окно целиком неудачное — курсор уже сдвинут на наибольший id окна, и мы шлём
    следующее окно, и так далее, пока не поймаем заявку.

    МЕХАНИЗМЫ ЗАЩИТЫ от «убегания вперёд» (чтобы всегда быть в актуальном цикле):
      • не предсказываем, когда РЕАЛЬНАЯ очередь полна — нет свободного слота, и
        курсор незачем отрывать от реальности;
      • если реальность обогнала курсор (появились заявки с бо́льшими id) —
        догоняем её (re-anchor), а не долбим неактуальные id;
      • не забегаем дальше PREDICTIVE_TAKE_LOOKAHEAD от реально увиденного id.
    Плюс спекулятивные отправки помечаются в ws.predicted_inflight и НЕ считаются
    занятыми слотами — поэтому опрос очереди (_poll_loop) не «слепнет» во время
    пачки предсказаний.
    """
    last_notice = 0.0
    # Якорь, про который уже сообщили в Telegram. Пока не появится НОВАЯ реальная
    # заявка (наблюдение сдвинет observed_anchor), повторять уведомление не нужно —
    # иначе в чат летело бы одно и то же сообщение и выглядело как дубли.
    notified_anchor: int | None = None
    while ws.running and ws.mode == "take":
        if not cfg.predictive_take or ws.last_order_id <= 0 or not ws.last_csrf_token:
            await asyncio.sleep(0.2)
            continue

        gap = cfg.predictive_take_gap_ms / 1000

        # Реальная очередь заполнена — предсказывать некуда: ждём, пока воркер
        # возьмёт/освободит реальные слоты. Курсор при этом не отрывается вперёд.
        if ws.slots_taken(inflight, ws.predicted_inflight) >= cfg.target_queue_size:
            await asyncio.sleep(gap or 0.2)
            continue

        # Реальность обогнала курсор — догоняем её, чтобы не «застрять» позади
        # и не тратить запросы на уже неактуальные (меньшие) id.
        if ws.observed_anchor > ws.last_order_id:
            ws.last_order_id = ws.observed_anchor

        look = cfg.predictive_take_lookahead
        if look > 0 and ws.observed_anchor > 0 and ws.last_order_id - ws.observed_anchor > look:
            # Дошли до предела забегания вперёд. РАНЬШЕ здесь просто ждали новую
            # заявку — и заявки ТЕРЯЛИСЬ: id, который мы уже пробовали, когда он
            # ещё НЕ существовал, повторно не пробовался (один «прочёс» вперёд —
            # одна попытка на id). Теперь возвращаемся к якорю и заново обстреливаем
            # тот же диапазон (retry): так ловим заявку, чей id появился ПОЗЖЕ нашей
            # первой попытки. Пауза = PREDICTIVE_TAKE_GAP_MS; при 0 берём небольшой пол.
            ws.last_order_id = ws.observed_anchor
            ws.predictive_sent.clear()
            await asyncio.sleep(gap or PREDICTIVE_RETRY_MIN_S)
            continue

        window_start = ws.last_order_id
        tasks = _fire_predicted_window(
            client, cfg, account, ws, notify, metrics, inflight, take_tasks
        )
        if tasks:
            # Сообщаем в Telegram, что превентивные POST реально уходят, но ТОЛЬКО
            # один раз на каждый новый реально увиденный id (якорь). Так видно, что
            # предиктив жив, но чат не забивается одинаковыми сообщениями. Троттлинг
            # оставлен как доп. предохранитель от частой смены якоря.
            if (
                cfg.debug
                and notify is not None
                and ws.observed_anchor != notified_anchor
                and time.monotonic() - last_notice >= PREDICTIVE_NOTIFY_MIN_S
            ):
                notified_anchor = ws.observed_anchor
                last_notice = time.monotonic()
                window_end = ws.last_order_id
                await notify(
                    f"🔮 Превентивно: {len(tasks)} POST take_payout "
                    f"(окно #{window_start + 1}…#{window_end}, якорь #{ws.observed_anchor})"
                )
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            if any(o is True for o in outcomes):
                ws.predictive_rounds = 0
            else:
                ws.predictive_rounds += 1

        if gap:
            await asyncio.sleep(gap)


async def _poll_loop(
    client: PayoutClient,
    cfg: Config,
    account,
    ws: WorkerState,
    notify,
    notify_file,
    metrics: Metrics,
    inflight: set,
    take_tasks: set,
) -> None:
    """Одна независимая воронка опроса очереди (их может быть несколько параллельно).

    Как только заявка увидена, POST уходит отдельной задачей, а цикл НЕ ЖДЁТ её
    ответа и сразу делает следующий GET — сетевой RTT отправки перекрывается
    следующим опросом, а не удлиняет паузу между опросами.
    """
    prev_get = 0.0
    while ws.running and ws.mode == "take":
        # Занятые слоты: заявки «в работе» на площадке + наши неподтверждённые
        # отправки. Спекулятивные (предсказанные) отправки НЕ считаем — иначе
        # пачка предсказаний выглядела бы как «очередь полна» и опрос реальной
        # очереди замирал бы (мы переставали видеть актуальные заявки).
        reserved = ws.slots_taken(inflight, ws.predicted_inflight)
        if reserved >= cfg.target_queue_size:
            ws.last_status = f"take(http): очередь полная {reserved}/{cfg.target_queue_size}"
            await asyncio.sleep(_poll_wait(cfg))
            continue

        ws.last_status = f"take(http): очередь {reserved}/{cfg.target_queue_size}, жду заявку"

        t_sent = time.perf_counter()
        try:
            html = await client.fetch_queue()
        except SessionExpired:
            ws.logged_in = False
            ws.running = False
            await notify("⚠️ Сессия слетела во время взятия заявок. Останавливаю. Запусти кабинет заново (/run).")
            return
        except BlockedError as exc:
            # Блокировка (403/антибот, Cloudflare): останавливаем взятие и просим
            # run_worker сбросить cookies и переавторизоваться (needs_reauth).
            ws.needs_reauth = True
            ws.running = False
            await notify(f"⛔ {exc}")
            return
        except Exception as exc:
            logger.warning("[%s] take GET failed: %s", account.name, exc)
            await asyncio.sleep(1)
            continue
        t_recv = time.perf_counter()
        rows = parse_rows(html)
        t_parsed = time.perf_counter()

        timing = {
            "poll_rtt_ms": round((t_recv - t_sent) * 1000, 2),
            "parse_ms": round((t_parsed - t_recv) * 1000, 2),
            "since_prev_get_ms": round((t_recv - prev_get) * 1000, 2) if prev_get else None,
        }
        prev_get = t_recv

        # Наблюдение очереди: запоминаем csrf_token и якорь (макс. увиденный id).
        # Само превентивное взятие будущих id делает отдельный цикл predict_loop.
        _observe_queue(ws, rows)

        # Берём первую заявку, которой ещё нет в работе и которую мы не отправляли.
        # Проверка и пометка inflight идут БЕЗ await, значит атомарны для event loop —
        # поэтому даже несколько параллельных воронок не создадут дублей.
        # ws.demands_ids — заявки, которые уже в работе на площадке (в т.ч. автовыдача
        # и оставшиеся с прошлого запуска): их повторно не берём.
        candidate = next(
            (
                r
                for r in rows
                if r["id"] not in inflight
                and r["id"] not in ws.demands_ids
                and r["id"] not in ws.taken_order_ids
                and r.get("csrf_token")
            ),
            None,
        )
        if candidate is None:
            await asyncio.sleep(_poll_wait(cfg))
            continue

        inflight.add(candidate["id"])
        task = asyncio.create_task(
            _do_take(client, cfg, account, ws, notify, metrics, candidate, timing, t_recv, inflight)
        )
        take_tasks.add(task)
        task.add_done_callback(take_tasks.discard)
        # Не ждём ответ на POST: даём задаче стартовать и сразу идём на следующий
        # GET — сетевой RTT отправки перекрывается следующим опросом очереди.
        await asyncio.sleep(0)


async def resync_demands_loop(client: PayoutClient, cfg: Config, ws: WorkerState, notify) -> None:
    """Свежая сверка со страницей demands ПО ЗАПРОСУ (/reset_session).

    /reset_session очистил кэш очереди (taken_order_ids/demands_ids/taken_count)
    и поставил ws.resync_required. Пока этот флаг поднят, слоты считаются пустыми
    (см. WorkerState.slots_taken) — этот цикл обязан первым делом перечитать
    demands и дать правду о занятых слотах, иначе воркер наберёт заявок сверх
    TARGET_QUEUE_SIZE на «пустом» кэше.
    """
    while ws.resync_required:
        try:
            rows = await fetch_demand_rows(client, ws)
        except SessionExpired:
            return  # сессию чинит run_worker (start_session/ensure_session)
        if rows is not None:
            ws.demands_ids = in_work_ids(rows)
            # Стираем «мёртвые» id из памяти вместе со сверкой — это и есть
            # лечение накопленного кэша (бот писал «в работе 10», реально 1).
            ws.taken_order_ids.intersection_update(ws.demands_ids)
            ws.resync_required = False
            free = max(0, cfg.target_queue_size - len(ws.demands_ids))
            await notify(
                f"🔄 /reset_session: очередь пересчитана по площадке. "
                f"В работе сейчас {len(ws.demands_ids)}/{cfg.target_queue_size}"
                f"{': ' + _ids_text(ws.demands_ids) if ws.demands_ids else ''}. "
                f"Возьму ещё {free}."
            )
            return
        # Страница недоступна — не зацикливаемся с бешеным ретраем:
        # notified_no in 1s, повторная попытка через DEMANDS_SYNC_S (см. demands_loop).
        await notify(
            "⚠️ /reset_session: страница demands пока недоступна — повторить "
            f"попытку через {cfg.demands_sync_s} с."
        )
        for _ in range(max(1, cfg.demands_sync_s)):
            if not ws.resync_required:
                return
            await asyncio.sleep(1)


async def take_loop(
    client: PayoutClient, cfg: Config, account, ws: WorkerState, notify, notify_file, metrics: Metrics
) -> None:
    """HTTP-режим взятия для одного кабинета.

    При TAKE_PARALLEL_POLLERS>1 очередь опрашивают несколько независимых воронок
    одновременно — это уменьшает окно обнаружения новой заявки. Дедупликация взятий
    берётся на себя общим inflight-набором.

    Замеры (см. Metrics): poll_rtt_ms (GET по сети), parse_ms (разбор HTML),
    since_prev_get_ms (окно между опросами), saw_to_sent_ms (увидели->отправили),
    py_overhead_ms (клиент), submit_rtt_ms (POST по сети), плюс отдельная запись
    take_result (confirmed — взяли или упустили).
    """
    configured_pollers = max(1, cfg.take_parallel_pollers)
    pollers = _effective_pollers(cfg)
    if pollers != configured_pollers:
        logger.info(
            "[%s] предиктив включён: воронок опроса %d → %d",
            account.name, configured_pollers, pollers,
        )
    await notify(
        f"⚡ HTTP-режим взятия запущен (без браузера). Цель: {cfg.target_queue_size} заявок, "
        f"воронок опроса: {pollers}."
    )
    # Сначала сверяемся с площадкой: сколько заявок УЖЕ в работе. Очередь в памяти
    if ws.resync_required:
        # /reset_session очистил кэш, пока мы были в фоне: приводим в соответствие
        # с площадкой прямо сейчас, дальше уже по обычному пути сверки.
        await resync_demands_loop(client, cfg, ws, notify)
    # Снимок ДО запроса: заявку, ПОДТВЕРЖДЁННУЮ во время этого запроса (её нет в
    # свежем HTML, сайт ещё не отдал), не считаем «мёртвым мусором» и не удаляем.
    taken_before = set(ws.taken_order_ids)
    try:
        rows = await fetch_demand_rows(client, ws)
    except SessionExpired:
        ws.logged_in = False
        ws.running = False
        await notify("⚠️ Сессия слетела на сверке с площадкой. Останавливаю. Запусти кабинет заново (/run).")
        return
    if rows is None:
        await notify(
            "⚠️ Страница demands недоступна — считаю очередь по памяти "
            "(после перезапуска возможен перебор заявок)."
        )
    else:
        # Приводим и площадку, и локальный кэш к одной картине. Ключевой момент:
        # старые id из ОПЕРАТИВНОЙ памяти (taken_order_ids), которых больше нет
        # на demands, выкидываем — без этого после перезапусков «мёртвые» заявки
        # копятся и бот пишет «в работе 10» при реальной 1.
        in_work = in_work_ids(rows)
        stale_taken = taken_before - in_work
        if stale_taken and cfg.debug:
            await notify(
                f"🧹 Память: убираю {len(stale_taken)} заявок, которых уже нет "
                f"в работе на площадке: {_ids_text(stale_taken)}."
            )
        ws.taken_order_ids.difference_update(stale_taken)
        ws.demands_ids = in_work
        free = max(0, cfg.target_queue_size - len(ws.demands_ids))
        if cfg.debug:
            await notify(
                f"📊 Сверка с площадкой: в работе уже "
                f"{len(ws.demands_ids)}/{cfg.target_queue_size} заявок"
                f"{': ' + _ids_text(ws.demands_ids) if ws.demands_ids else ''}. "
                f"Возьму ещё {free}."
            )
    status_task = asyncio.create_task(demands_loop(client, cfg, ws, notify))
    inflight: set = set()
    take_tasks: set = set()
    poll_tasks = [
        asyncio.create_task(
            _poll_loop(client, cfg, account, ws, notify, notify_file, metrics, inflight, take_tasks)
        )
        for _ in range(pollers)
    ]
    # Живой ответ на /reset_session: пока флаг поднят, слоты считаются пустыми —
    # воркер должен сделать свежую сверку с площадкой и только потом брать заявки.
    resync_task = asyncio.create_task(resync_demands_loop(client, cfg, ws, notify))
    # Превентивный поиск будущих id: отдельный фон, шагающий вперёд окнами.
    predict_task = asyncio.create_task(
        predict_loop(client, cfg, account, ws, notify, metrics, inflight, take_tasks)
    )
    try:
        await asyncio.gather(*poll_tasks)
    finally:
        status_task.cancel()
        resync_task.cancel()
        predict_task.cancel()
        try:
            await status_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await resync_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await predict_task
        except (asyncio.CancelledError, Exception):
            pass
        # Гасим незавершённые взятия до закрытия клиента (иначе — "client closed").
        for task in list(take_tasks):
            task.cancel()
        if take_tasks:
            await asyncio.gather(*take_tasks, return_exceptions=True)


async def _run_hot_loop(cfg: Config, account, ws: WorkerState, notify, notify_file, metrics, cookies) -> None:
    """Один заход горячего цикла на готовых cookies. Возвращается, когда цикл сам
    завершился (ws.running=False внутри) — например, после блокировки/сбоя сессии."""
    client = PayoutClient(cfg, cookies)
    ws.logged_in = True
    try:
        await notify("🚀 HTTP-цикл запущен (браузер в горячем цикле не используется).")
        if ws.mode == "watch":
            await watch_loop(client, cfg, account, ws, notify, notify_file, metrics)
        else:
            await take_loop(client, cfg, account, ws, notify, notify_file, metrics)
    finally:
        await client.aclose()
        ws.logged_in = False


async def _recover_from_block(cfg: Config, account, ws: WorkerState, notify, notify_file) -> bool:
    """Сброс cookies + повторная браузерная авторизация после блокировки (403/антибот).

    Удаляет сохранённую сессию кабинета (auth.json) и проходит авторизацию с нуля:
    свежий cf_clearance обычно снимает блок. Число подряд неудачных переавторизаций
    ограничено MAX_BLOCK_REAUTH, чтобы не зациклиться. Возвращает True, если удалось
    получить рабочие cookies.
    """
    if ws.reauth_attempts >= MAX_BLOCK_REAUTH:
        await notify(
            f"⛔ Сайт блокирует запросы, переавторизация не помогла "
            f"({ws.reauth_attempts}/{MAX_BLOCK_REAUTH}). Останавливаюсь — проверьте "
            "кабинет вручную или вернитесь на WORKER_BACKEND=browser."
        )
        return False

    ws.reauth_attempts += 1
    had_file = _reset_stored_session(account)
    await notify(
        "♻️ Сбрасываю сохранённые cookies"
        f"{' (файл сессии удалён)' if had_file else ' (файла сессии не было)'} "
        f"и прохожу авторизацию заново — попытка {ws.reauth_attempts}/{MAX_BLOCK_REAUTH}…"
    )
    try:
        cookies = await ensure_session(cfg, account, ws, notify, notify_file, force_reauth=True)
    except BlockedError as exc:
        await notify(f"⛔ Повторная авторизация тоже заблокирована: {exc}")
        return False
    return bool(cookies)


async def run_worker(cfg: Config, account, ws: WorkerState, notify, notify_file) -> None:
    """Воркер ОДНОГО кабинета. Живёт постоянно: ждёт ws.running=True, чтобы начать работу.

    Горячий цикл — на httpx (без браузера). Браузер поднимается только если
    сохранённая сессия кабинета недействительна, и только для разовой авторизации.
    При блокировке (403/антибот) воркер сам сбрасывает cookies кабинета и проходит
    авторизацию заново (см. _recover_from_block).
    Все уведомления помечаются именем кабинета, чтобы было видно, чья это заявка.
    """
    metrics = Metrics(cfg)

    async def acct_notify(text: str):
        await notify(f"[{account.name}] {text}")

    async def acct_notify_file(path: str, caption: str = ""):
        await notify_file(path, f"[{account.name}] {caption}".strip())

    while True:
        if not ws.running:
            await asyncio.sleep(1)
            continue

        try:
            cookies = await ensure_session(cfg, account, ws, acct_notify, acct_notify_file)
        except BlockedError as exc:
            await acct_notify(f"⛔ {exc}")
            cookies = None
            ws.needs_reauth = True

        # Проверка сессии или горячий цикл упёрлись в блокировку: сбрасываем
        # cookies кабинета и проходим авторизацию заново.
        if ws.needs_reauth:
            ws.needs_reauth = False
            if not await _recover_from_block(cfg, account, ws, acct_notify, acct_notify_file):
                ws.running = False
            continue

        if not cookies:
            ws.running = False
            continue

        t_started = time.monotonic()
        await _run_hot_loop(cfg, account, ws, acct_notify, acct_notify_file, metrics, cookies)

        # Если цикл реально проработал какое-то время, значит блок позади — только
        # тогда обнуляем счётчик переавторизаций (иначе можно зациклиться на
        # быстрых повторных блокировках).
        if time.monotonic() - t_started > 30:
            ws.reauth_attempts = 0

        # Горячий цикл мог остановиться из-за блокировки — переавторизуемся и продолжим.
        if ws.needs_reauth:
            ws.needs_reauth = False
            if not await _recover_from_block(cfg, account, ws, acct_notify, acct_notify_file):
                ws.running = False
            continue





