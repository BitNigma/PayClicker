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
    """Антибот (DataDome) отдал challenge вместо страницы, вместо обычного HTML."""


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


def _classify(html: str, url: str) -> str:
    """ok | signin | blocked — по содержимому ответа и итоговому URL."""
    low = html.lower()
    if "captcha-delivery.com" in low or "datadome" in low or "geo.captcha" in low:
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
        if kind == "blocked":
            raise BlockedError(
                "Сайт отдал антибот-заглушку (DataDome) вместо страницы: HTTP-клиент "
                "распознан. Нужно обновить cookies через браузерную авторизацию "
                "(или вернуться на WORKER_BACKEND=browser)."
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


async def ensure_session(cfg: Config, account, ws: WorkerState, notify) -> dict | None:
    """Возвращает рабочие cookies кабинета. Если сохранённые не годятся — разово
    поднимает браузер, чтобы обновить auth.json кабинета (вне горячего цикла)."""
    cookies = load_cookies(account)
    if cookies:
        probe = PayoutClient(cfg, cookies)
        try:
            if await _session_ok(probe):
                return cookies
        finally:
            await probe.aclose()

    await notify("🔐 Cookies недействительны — разовая браузерная авторизация (вне горячего цикла)...")
    from .browser_worker import login_via_browser  # локальный импорт: браузер нужен только тут

    ok = await login_via_browser(cfg, account, ws, notify)
    if not ok:
        return None
    return load_cookies(account) or None


def _dump_take_request(cfg: Config, order_id: str, data: dict, response: httpx.Response) -> Path:
    """Сохраняет фактический HTTP POST и ответ — для сверки с версией на браузере."""
    d = Path(cfg.log_path).parent
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"take_request_http_{order_id}_{int(time.time())}.json"
    payload = {
        "transport": "httpx",
        "request": {
            "url": cfg.queue_url,
            "method": "POST",
            "headers": {"content-type": "application/x-www-form-urlencoded"},
            "post_data": "&".join(f"{k}={v}" for k, v in data.items()),
        },
        "response": {"url": str(response.url), "status": response.status_code},
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
) -> None:
    """Один такт по заявке: POST -> метрика -> подтверждение -> результат.

    Запускается отдельной задачей, чтобы НЕ блокировать следующий GET очереди:
    сетевой RTT отправки POST перекрывается следующим опросом (это убирает
    ~150-270 мс «слепоты» на каждое взятие).
    """
    order_id = row["id"]
    csrf_token = row.get("csrf_token")
    access_key = row.get("access_key", "")
    try:
        t_post = time.perf_counter()
        try:
            response = await client.take_payout(order_id, csrf_token, access_key)
        except Exception as exc:
            logger.warning("[%s] POST take_payout для #%s не удался: %s", account.name, order_id, exc)
            return
        t_post_done = time.perf_counter()

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
        )

        # Дамп POST-запроса сохраняем только локально (./data), в чат НЕ отправляем,
        # чтобы не засорять переписку.
        if cfg.debug_capture_attempts:
            try:
                data = {
                    "csrf_token": csrf_token,
                    "act": "take_payout",
                    "payout_id": order_id,
                    "access_key": access_key,
                }
                _dump_take_request(cfg, order_id, data, response)
            except Exception:
                logger.exception("Не удалось сохранить дамп запроса для #%s", order_id)

        t_confirm = time.perf_counter()
        try:
            confirmed = await _confirm_taken(client, cfg, order_id)
        except SessionExpired:
            confirmed = False
        metrics.record(
            event="take_result",
            account=account.name,
            account_index=account.index,
            order_id=order_id,
            confirmed=confirmed,
            confirm_ms=round((time.perf_counter() - t_confirm) * 1000, 2),
        )
        if confirmed:
            ws.taken_count += 1
            ws.taken_order_ids.add(order_id)
            # Заявка сразу занимает слот: не ждём следующей сверки с demands.
            ws.demands_ids.add(order_id)
            await notify(f"✅ Взята заявка #{order_id} (HTTP)")
            await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg, ws)}")
        else:
            await notify(f"⚠️ Заявку #{order_id} не удалось подтвердить — похоже, её забрали раньше нас.")
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
                    await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg, ws)}")
            # Заявки, которых мы не брали (автовыдача площадки) — тоже занимают слот.
            auto = sorted(in_work - stale - ws.taken_order_ids)
            if auto:
                await notify(
                    f"📦 Автовыдача: {_ids_text(auto)} уже в работе "
                    f"(площадка выдала без нашего взятия)."
                )
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
        await notify(
            f"📊 Сверка с площадкой: в работе уже "
            f"{len(ws.demands_ids)}/{cfg.target_queue_size} заявок "
            f"(цель задаётся TARGET_QUEUE_SIZE)."
        )
    prev_ids: set = set()

    while ws.running and ws.mode == "watch":
        t_sent = time.perf_counter()
        try:
            html = await client.fetch_queue()
        except SessionExpired:
            ws.logged_in = False
            ws.running = False
            await notify("⚠️ Сессия слетела. Останавливаю наблюдение. Запусти кабинет заново (/run).")
            break
        except BlockedError as exc:
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
        # Занятые слоты: заявки «в работе» на площадке + наши неподтверждённые отправки.
        reserved = ws.slots_taken(inflight)
        if reserved >= cfg.target_queue_size:
            ws.last_status = f"take(http): очередь полная {reserved}/{cfg.target_queue_size}"
            await asyncio.sleep(_sleep_ms(cfg.take_poll_min_ms, cfg.take_poll_max_ms))
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
            await asyncio.sleep(_sleep_ms(cfg.take_poll_min_ms, cfg.take_poll_max_ms))
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
    pollers = max(1, cfg.take_parallel_pollers)
    await notify(
        f"⚡ HTTP-режим взятия запущен (без браузера). Цель: {cfg.target_queue_size} заявок, "
        f"воронок опроса: {pollers}."
    )
    # Сначала сверяемся с площадкой: сколько заявок УЖЕ в работе. Очередь в памяти
    # живёт только до перезапуска процесса, а часть заявок сайт выдаёт автоматически —
    # без этой сверки после рестарта бот набирает лишние заявки сверх цели.
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
        ws.demands_ids = in_work_ids(rows)
        free = max(0, cfg.target_queue_size - len(ws.demands_ids))
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
    try:
        await asyncio.gather(*poll_tasks)
    finally:
        status_task.cancel()
        try:
            await status_task
        except (asyncio.CancelledError, Exception):
            pass
        # Гасим незавершённые взятия до закрытия клиента (иначе — "client closed").
        for task in list(take_tasks):
            task.cancel()
        if take_tasks:
            await asyncio.gather(*take_tasks, return_exceptions=True)


async def run_worker(cfg: Config, account, ws: WorkerState, notify, notify_file) -> None:
    """Воркер ОДНОГО кабинета. Живёт постоянно: ждёт ws.running=True, чтобы начать работу.

    Горячий цикл — на httpx (без браузера). Браузер поднимается только если
    сохранённая сессия кабинета недействительна, и только для разовой авторизации.
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
            cookies = await ensure_session(cfg, account, ws, acct_notify)
        except BlockedError as exc:
            await acct_notify(f"⛔ {exc}")
            ws.running = False
            continue

        if not cookies:
            ws.running = False
            continue

        client = PayoutClient(cfg, cookies)
        ws.logged_in = True
        try:
            await acct_notify("🚀 HTTP-цикл запущен (браузер в горячем цикле не используется).")
            if ws.mode == "watch":
                await watch_loop(client, cfg, account, ws, acct_notify, acct_notify_file, metrics)
            else:
                await take_loop(client, cfg, account, ws, acct_notify, acct_notify_file, metrics)
        finally:
            await client.aclose()
            ws.logged_in = False





