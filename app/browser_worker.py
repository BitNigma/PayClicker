import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

from .config import Config
from .state import state

logger = logging.getLogger("worker")

# =========================================================================
# СЕЛЕКТОРЫ. Вход и 2FA — по реальной разметке spark-trading.cash.
# Страница выплат недоступна без авторизации — её селекторы ещё нужно заполнить.
# При ошибке бот сохраняет скриншот и HTML в ./data/debug_*.png|html.
# =========================================================================
SEL_LOGIN_INPUT = "form.auth-form input[name='email']"
SEL_PASSWORD_INPUT = "form.auth-form input[name='pass']"
SEL_LOGIN_SUBMIT = "form.auth-form:has(input[name='pass']) button.auth-submit"

# Форма 2FA (показывается на /signin после ввода email/пароля)
SEL_2FA_INPUT = "#form_tg input[name='code']"
SEL_2FA_SUBMIT = "#form_tg button.auth-submit"

# Таблица заявок на странице выплат (по присланной разметке)
SEL_PAYOUT_CARD = ".payout-card"
SEL_PAYOUT_ROWS = ".payout-card table.payout-table tbody tr"
EMPTY_ROW_TEXT = "не найдено"   # строка-заглушка, когда заявок нет

# Разбор строки заявки (общая разметка и на payouts_choice, и на demands)
SEL_ROW_ID = "td.payout-status b"                      # текст вида "# 6408416"
SEL_ROW_STATUS_BADGE = "td.payout-status span.badge"    # текст вида "🕛 Ждет провайдера"
SEL_TAKE_BUTTON = "form:has(input[name='act'][value='take_payout']) button[type='submit']"

STATUS_DONE_MARKER = "выполнена"   # если это есть в бейдже статуса — заявка закрыта, слот свободен

# ТУТ НУЖНО ЗАПОЛНИТЬ по странице выплат (payouts_choice) — пока заглушки:
# =========================================================================


async def _send_live_snapshot(page: Page, cfg: Config, notify, notify_file, label: str):
    """Мгновенный снимок того, что бот видит СЕЙЧАС: скриншот + HTML + URL.
    Нужен, чтобы вручную сверить с тем, что видно в обычном браузере, и понять,
    застрял ли бот на неверной странице/сессии, или заявок правда нет."""
    d = Path(cfg.log_path).parent
    d.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    try:
        url = page.url
        shot_path = d / f"snapshot_{label}_{ts}.png"
        html_path = d / f"snapshot_{label}_{ts}.html"
        await page.screenshot(path=str(shot_path), full_page=True)
        html_path.write_text(await page.content(), encoding="utf-8")
        try:
            rows = await _get_payout_rows(page)
            rows_info = f"строк с заявками: {len(rows)}"
        except Exception:
            rows_info = "не удалось посчитать строки"
        await notify(f"📸 Снимок [{label}]\nURL: {url}\n{rows_info}")
        await notify_file(str(shot_path), f"Скриншот [{label}]")
        await notify_file(str(html_path), f"HTML [{label}]")
    except Exception as e:
        logger.exception("snapshot failed")
        await notify(f"⚠️ Не удалось сделать снимок [{label}]: {e}")


async def _dump_debug(page: Page, cfg: Config, name: str):
    """Сохраняет скриншот и HTML страницы для отладки селекторов."""
    d = Path(cfg.log_path).parent
    d.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(d / f"debug_{name}.png"), full_page=True)
        (d / f"debug_{name}.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        logger.exception("debug dump failed")


def _log_action(cfg: Config, order_id: str, action: str):
    Path(cfg.log_path).parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "order_id": order_id, "action": action}
    with open(cfg.log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# Авторизация кабинета поднимает отдельный Chromium и может требовать ручного
# ввода 2FA, поэтому при мультиаккаунте запускаем их строго по одной.
_LOGIN_LOCK: asyncio.Lock | None = None


def _login_lock() -> asyncio.Lock:
    """Ленивое создание блокировки: она должна рождаться внутри работающего
    event loop, иначе на некоторых версиях Python будет «no current event loop»."""
    global _LOGIN_LOCK
    if _LOGIN_LOCK is None:
        _LOGIN_LOCK = asyncio.Lock()
    return _LOGIN_LOCK


async def _build_context(pw, cfg: Config, account) -> BrowserContext:
    browser = await pw.chromium.launch(
        headless=cfg.headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    storage_path = Path(account.storage_state_path)
    if storage_path.exists():
        context = await browser.new_context(storage_state=str(storage_path))
    else:
        context = await browser.new_context()
    return browser, context


def _on_signin(page: Page) -> bool:
    return "/signin" in page.url


async def ensure_logged_in(page: Page, context: BrowserContext, cfg: Config, account, ws, notify) -> bool:
    # Пробуем открыть рабочую страницу: если сессия жива, редиректа на /signin не будет
    await page.goto(cfg.queue_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    if not _on_signin(page):
        ws.logged_in = True
        await notify("✅ Сессия активна, повторная авторизация не нужна.")
        return True

    await notify("🔐 Начинаю авторизацию на сайте...")
    await page.goto(cfg.site_url, wait_until="domcontentloaded")
    try:
        await page.fill(SEL_LOGIN_INPUT, account.login, timeout=10000)
        await page.fill(SEL_PASSWORD_INPUT, account.password, timeout=10000)
        await page.click(SEL_LOGIN_SUBMIT, timeout=10000)
    except Exception as e:
        await _dump_debug(page, cfg, "login_form")
        await notify(f"⚠️ Не удалось заполнить форму входа: {e}\nСмотри ./data/debug_login_form.png/.html")
        return False

    # Ждём: либо уйдём со /signin (без 2FA), либо появится поле кода
    code_input = None
    for _ in range(30):  # ~15 сек
        await page.wait_for_timeout(500)
        if not _on_signin(page):
            break
        try:
            if await page.query_selector(SEL_2FA_INPUT):
                code_input = True
                break
        except Exception:
            continue  # страница как раз перезагружается — повторим на следующей итерации

    if code_input:
        await notify("📲 Введите код 2FA ответным сообщением в этот чат (5 минут).")
        try:
            code = await ws.wait_for_2fa_code(timeout=300)
        except asyncio.TimeoutError:
            await notify("⏱ Код не получен за 5 минут. Отправьте /start_session заново.")
            return False
        try:
            await page.fill(SEL_2FA_INPUT, code, timeout=10000)
        except Exception as e:
            await _dump_debug(page, cfg, "2fa_form")
            await notify(f"⚠️ Ошибка при вводе 2FA: {e}\nСмотри ./data/debug_2fa_form.png/.html")
            return False

        # Сайт может сам отправить форму сразу после ввода кода ("Проверка кода...")
        # и уйти на другую страницу — тогда клик не нужен, а обращение к DOM
        # падает с "Execution context was destroyed". Это нормально, игнорируем.
        try:
            await page.click(SEL_2FA_SUBMIT, timeout=3000)
        except Exception as e:
            logger.info("2FA submit click skipped (форма уже отправлена?): %s", e)

        try:
            await page.wait_for_url(lambda u: "/signin" not in u, timeout=15000)
        except Exception:
            pass

    # Если сайт остался на /signin, хотя код принят — сессия обычно уже создана.
    # Принудительно открываем рабочую страницу и проверяем ещё раз.
    if _on_signin(page):
        await page.goto(cfg.queue_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

    if _on_signin(page):
        await _dump_debug(page, cfg, "after_login")
        await notify("❌ Всё ещё на странице входа: неверный пароль/код или другие селекторы. "
                     "Смотри ./data/debug_after_login.png/.html")
        return False

    Path(account.storage_state_path).parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=account.storage_state_path)
    ws.logged_in = True
    await notify("✅ Авторизация успешна, сессия сохранена.")
    return True


async def login_via_browser(cfg: Config, account, ws, notify) -> bool:
    """Разовая браузерная авторизация (в т.ч. 2FA) — нужна ТОЛЬКО чтобы получить
    и обновить cookies кабинета в его storage_state_path для HTTP-горячего цикла
    (http_worker). Сам горячий цикл браузер не использует.

    При мультиаккаунте авторизации идут строго по одной (см. _login_lock):
    параллельные Chromium'ы с ручным вводом 2FA путали бы, какой код чей,
    и съедали RAM.
    """
    lock = _login_lock()
    if lock.locked():
        await notify("⏳ Другой кабинет сейчас авторизуется — жду своей очереди...")
    async with lock:
        return await _login_via_browser_locked(cfg, account, ws, notify)


async def _login_via_browser_locked(cfg: Config, account, ws, notify) -> bool:
    """Сама авторизация; вызывать только под _login_lock()."""
    async with async_playwright() as pw:
        browser, context = await _build_context(pw, cfg, account)
        try:
            page = await context.new_page()
            return await ensure_logged_in(page, context, cfg, account, ws, notify)
        finally:
            await browser.close()



def _register_dialog_autoaccept(page: Page):
    """Сайт использует нативный confirm() после клика «Забрать выплату» —
    подтверждаем его автоматически и мгновенно, иначе заявку успеют забрать конкуренты."""
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))


async def _parse_row(row) -> dict:
    order_id = None
    status = ""
    try:
        id_el = await row.query_selector(SEL_ROW_ID)
        if id_el:
            txt = (await id_el.inner_text()).strip()
            order_id = txt.lstrip("#").strip()
    except Exception:
        pass
    try:
        badge_el = await row.query_selector(SEL_ROW_STATUS_BADGE)
        if badge_el:
            status = (await badge_el.inner_text()).strip()
    except Exception:
        pass
    return {"id": order_id, "status": status}


async def _get_payout_rows(page: Page):
    """Строки заявок (без строки-заглушки «Не найдено»), уже распарсенные."""
    raw_rows = await page.query_selector_all(SEL_PAYOUT_ROWS)
    result = []
    for row in raw_rows:
        try:
            text = (await row.inner_text()).strip().lower()
        except Exception:
            continue
        if not text or EMPTY_ROW_TEXT in text:
            continue
        parsed = await _parse_row(row)
        parsed["el"] = row
        result.append(parsed)
    return result


STATUS_IN_WORK_MARKER = "в работе у провайдера"  # пока статус содержит это — заявка ещё выполняется


def _in_work_ids(rows) -> set:
    """id заявок, которые занимают слот целевой очереди (статус «в работе у провайдера»).

    На странице demands это и взятые нами заявки, и выданные площадкой
    автоматически — место в очереди занимают обе категории.
    """
    return {
        r["id"]
        for r in rows
        if r.get("id") and STATUS_IN_WORK_MARKER in (r.get("status") or "").lower()
    }


async def _read_demand_rows(status_page: Page, cfg: Config) -> list[dict] | None:
    """Строки страницы demands (все статусы) или None, если страницу прочитать не удалось."""
    try:
        await status_page.goto(cfg.demands_url, wait_until="domcontentloaded")
        return await _get_payout_rows(status_page)
    except Exception:
        logger.exception("Не удалось прочитать demands для сверки очереди")
        return None


async def _sync_demands_on_start(status_page: Page, cfg: Config, notify) -> None:
    """Сверка с площадкой перед стартом взятия: сколько заявок УЖЕ в работе.

    Очередь в памяти живёт только до перезапуска процесса, а часть заявок сайт
    выдаёт автоматически (без нашего «Забрать выплату») — без этой сверки бот
    после рестарта набирает лишние заявки сверх TARGET_QUEUE_SIZE.
    """
    rows = await _read_demand_rows(status_page, cfg)
    if rows is None:
        await notify(
            "⚠️ Страница demands недоступна — считаю очередь по памяти "
            "(после перезапуска возможен перебор заявок)."
        )
        return
    in_work = _in_work_ids(rows)
    state.demands_ids = in_work
    ids_str = f" ({', '.join(f'#{i}' for i in sorted(in_work))})" if in_work else ""
    free = max(0, cfg.target_queue_size - len(in_work))
    await notify(
        f"📊 Сверка с площадкой: в работе уже {len(in_work)}/{cfg.target_queue_size} заявок"
        f"{ids_str}. Возьму ещё {free}."
    )


async def _confirm_taken(page: Page, cfg: Config, order_id: str, attempts: int = 6, delay: float = 0.5) -> bool:
    """Быстрая проверка (без долгих пауз), что заявка реально появилась на demands
    именно под нашим кликом, а не ушла конкуренту в последний момент."""
    for _ in range(attempts):
        try:
            await page.goto(cfg.demands_url, wait_until="domcontentloaded")
            rows = await _get_payout_rows(page)
            for r in rows:
                if r["id"] == order_id:
                    return True
        except Exception:
            logger.exception("Ошибка при подтверждении взятия заявки #%s", order_id)
        await asyncio.sleep(delay)
    return False


async def _setup_live_observer(page: Page, signal_queue: asyncio.Queue):
    """Вместо периодического reload — подписываемся на изменения DOM таблицы заявок
    через MutationObserver прямо в браузере. Сайт обновляет контент в реальном времени
    (свои вебсокеты/поллинг), и observer реагирует на это мгновенно, без сетевого
    круга через наш reload(). Наблюдатель переустанавливается после каждой навигации,
    так как новый документ = новый JS-контекст."""

    async def on_change(*_args):
        try:
            signal_queue.put_nowait(True)
        except asyncio.QueueFull:
            pass

    # expose_function можно вызвать один раз на странице — переживает последующие навигации
    try:
        await page.expose_function("__onQueueChange", on_change)
    except Exception:
        pass  # уже зарегистрирована на этой странице ранее

    injector = """
    () => {
        const target = document.querySelector(".payout-card table.payout-table tbody");
        if (!target || !window.__onQueueChange) return;
        if (window.__queueObserver) { window.__queueObserver.disconnect(); }
        window.__queueObserver = new MutationObserver(() => { window.__onQueueChange(); });
        window.__queueObserver.observe(target, { childList: true, subtree: true });
    }
    """

    async def inject():
        try:
            await page.evaluate(injector)
        except Exception:
            pass  # страница как раз перезагружается — переустановим на следующей загрузке

    await inject()
    page.on("load", lambda: asyncio.create_task(inject()))
    return inject


def _queue_snapshot_text(cfg: Config) -> str:
    """Снимок занятых слотов. Берём площадку (state.demands_ids), а не только память:
    после рестарта процесса взятые ранее заявки тоже занимают слот."""
    ids = sorted(state.demands_ids | state.taken_order_ids)
    if not ids:
        return f"Очередь пуста (0/{cfg.target_queue_size})."
    ids_str = ", ".join(f"#{i}" for i in ids)
    return f"В работе сейчас ({len(ids)}/{cfg.target_queue_size}): {ids_str}"


async def _debug_dump_attempt(cfg: Config, notify_file, order_id: str, stage: str, row_html: str = None, page: Page = None):
    """Отладочный снимок конкретной попытки взятия заявки: 'before' — HTML строки
    до клика, 'after' — HTML+скриншот страницы сразу после клика. Не блокирует
    основной цикл — вызывается как фоновая задача."""
    try:
        d = Path(cfg.log_path).parent
        d.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        if row_html is not None:
            fp = d / f"attempt_{order_id}_{ts}_{stage}.html"
            fp.write_text(row_html, encoding="utf-8")
            await notify_file(str(fp), f"Заявка #{order_id} — {stage}")
        if page is not None:
            html_fp = d / f"attempt_{order_id}_{ts}_{stage}.html"
            shot_fp = d / f"attempt_{order_id}_{ts}_{stage}.png"
            html_fp.write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(shot_fp), full_page=True)
            await notify_file(str(html_fp), f"Заявка #{order_id} — {stage} (HTML)")
            await notify_file(str(shot_fp), f"Заявка #{order_id} — {stage} (скриншот)")
    except Exception:
        logger.exception("debug dump attempt failed for #%s [%s]", order_id, stage)


async def _confirm_and_report(confirm_page: Page, confirm_lock: asyncio.Lock, cfg: Config, order_id: str, notify):
    """Подтверждение взятия вынесено в фоновую задачу: основной цикл не ждёт
    результат и сразу возвращается искать следующую заявку — это и есть
    главная экономия времени в гонке за заявками. Lock нужен, потому что
    confirm_page — общая вкладка: если клики идут часто, подтверждения не
    должны одновременно дёргать её навигацию и путать друг друга."""
    async with confirm_lock:
        confirmed = await _confirm_taken(confirm_page, cfg, order_id)
    if confirmed:
        state.taken_count += 1
        state.taken_order_ids.add(order_id)
        # Заявка сразу занимает слот: не ждём следующей сверки с demands.
        state.demands_ids.add(order_id)
        await notify(f"✅ Взята заявка #{order_id}")
        await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg)}")
    else:
        await notify(f"⚠️ Заявку #{order_id} не удалось подтвердить — похоже, её забрали раньше нас.")


async def _capture_and_submit_form(page: Page, row_el, cfg: Config, notify_file, order_id: str):
    """Отправляет форму 'Забрать выплату' напрямую через form.submit() — это
    ПОЛНОСТЬЮ обходит confirm() (он висит на onclick кнопки, а не на submit
    формы), так что диалог не появляется и не тратится время на его обработку.
    Заодно ловим реальный POST-запрос (заголовки, тело) и ответ сервера —
    полезно для отладки и на будущее, если решим слать запрос вообще без клика в DOM."""
    captured = {}
    done = asyncio.Event()

    def on_request(req):
        if req.method == "POST" and "request" not in captured:
            captured["request"] = {
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "post_data": req.post_data,
            }

    def on_response(res):
        if res.request.method == "POST" and "response" not in captured:
            captured["response"] = {"url": res.url, "status": res.status}
            done.set()

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        await row_el.evaluate(
            "row => { const form = row.querySelector('form'); if (form) form.submit(); }"
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    if cfg.debug_capture_attempts and captured:
        try:
            d = Path(cfg.log_path).parent
            d.mkdir(parents=True, exist_ok=True)
            fp = d / f"take_request_{order_id}_{int(time.time())}.json"
            fp.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
            # В чат НЕ отправляем, чтобы не засорять переписку — файл остаётся в ./data.
        except Exception:
            logger.exception("Не удалось сохранить captured request для #%s", order_id)


async def fast_take_loop(choice_page: Page, confirm_page: Page, cfg: Config, notify, notify_file):
    """Событийное взятие заявок: реагируем на изменение DOM почти мгновенно,
    без ожидания периодического reload. Дефицит считаем по данным площадки
    (state.demands_ids) плюс своя память, не опрашивая demands на каждой
    итерации: сверку раз в DEMANDS_SYNC_S секунд делает status_loop."""
    _register_dialog_autoaccept(choice_page)
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    confirm_lock = asyncio.Lock()

    await choice_page.goto(cfg.queue_url, wait_until="domcontentloaded")
    await _setup_live_observer(choice_page, signal_queue)

    await notify(
        f"⚡ Быстрый режим взятия запущен (события DOM + классический fallback-reload). "
        f"Цель: {cfg.target_queue_size} заявок в очереди."
    )

    def _next_reload_deadline() -> float:
        return time.monotonic() + random.uniform(cfg.take_poll_min_ms, cfg.take_poll_max_ms) / 1000

    next_reload_at = _next_reload_deadline()
    reload_count = 0

    while state.running and state.mode == "take":
        # Занятые слоты считаем по площадке (state.demands_ids): там и наши заявки,
        # и автовыдача, и оставшиеся с прошлого запуска процесса.
        slots = state.slots_taken()
        deficit = cfg.target_queue_size - slots
        if deficit <= 0:
            state.last_status = f"take: очередь полная {slots}/{cfg.target_queue_size}"
            try:
                await asyncio.wait_for(signal_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            if state.snapshot_requested:
                state.snapshot_requested = False
                await _send_live_snapshot(choice_page, cfg, notify, notify_file, "take")
            continue

        state.last_status = f"take: очередь {slots}/{cfg.target_queue_size}, жду заявку"

        # ждём сигнал об изменении DOM; короткий таймаут — подстраховка на случай,
        # если observer что-то пропустил (например, сразу после навигации).
        # Держим его маленьким (0.25с), чтобы не проигрывать гонку за заявку.
        try:
            await asyncio.wait_for(signal_queue.get(), timeout=0.25)
        except asyncio.TimeoutError:
            pass

        # Классический fallback: даже если DOM-событие ни разу не пришло (например,
        # сайт обновляет таблицу только через полноценный reload, а не "тихо" через JS),
        # периодически принудительно перезагружаем страницу — это гарантирует, что
        # мы не проспим заявку из-за того, что MutationObserver не на что реагировать.
        if time.monotonic() >= next_reload_at:
            try:
                await choice_page.reload(wait_until="domcontentloaded")
                await _setup_live_observer(choice_page, signal_queue)  # observer переживает reload, но на всякий случай
                reload_count += 1
                if reload_count == 1 or reload_count % 20 == 0:
                    await notify(f"🔁 Fallback-reload сработал уже {reload_count} раз(а) — наблюдение активно.")
            except Exception as e:
                logger.warning("Fallback reload не удался: %s", e)
            next_reload_at = _next_reload_deadline()

        if state.snapshot_requested:
            state.snapshot_requested = False
            await _send_live_snapshot(choice_page, cfg, notify, notify_file, "take")

        if _on_signin(choice_page):
            state.logged_in = False
            state.running = False
            await notify("⚠️ Сессия слетела во время взятия заявок. Останавливаю. Отправь /start_session.")
            break

        try:
            rows = await _get_payout_rows(choice_page)
        except Exception:
            continue
        if not rows:
            continue

        row = rows[0]
        order_id = row.get("id")
        if order_id and (order_id in state.demands_ids or order_id in state.taken_order_ids):
            # Верхняя строка очереди уже занимает слот (взяли мы или выдала
            # автовыдача, в т.ч. до перезапуска) — повторно форму не шлём.
            await asyncio.sleep(random.uniform(cfg.take_poll_min_ms, cfg.take_poll_max_ms) / 1000)
            continue
        row_html_before = None
        if cfg.debug_capture_attempts:
            try:
                row_html_before = await row["el"].evaluate("e => e.outerHTML")
            except Exception:
                row_html_before = None
            if order_id:
                asyncio.create_task(
                    _debug_dump_attempt(cfg, notify_file, order_id, "before_submit", row_html=row_html_before)
                )

        try:
            if cfg.debug_capture_attempts and order_id:
                # ловим реальный POST-запрос/ответ для отладки
                await _capture_and_submit_form(choice_page, row["el"], cfg, notify_file, order_id)
            else:
                # form.submit() напрямую — обходит confirm(), висящий на onclick кнопки
                await row["el"].evaluate(
                    "row => { const form = row.querySelector('form'); if (form) form.submit(); }"
                )
        except Exception as e:
            logger.warning("Отправка формы заявки #%s не удалась (строка могла устареть): %s", order_id, e)
            continue

        try:
            await choice_page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        if cfg.debug_capture_attempts and order_id:
            # тут НЕ фон: choice_page общая, и следующая итерация цикла может
            # начать новую навигацию раньше, чем мы успеем снять текущее состояние
            await _debug_dump_attempt(cfg, notify_file, order_id, "after_submit", page=choice_page)

        if order_id:
            asyncio.create_task(_confirm_and_report(confirm_page, confirm_lock, cfg, order_id, notify))
        # НЕ ждём результат подтверждения здесь — сразу возвращаемся в начало цикла
        # искать следующую заявку, пока конкуренты не забрали её.


async def status_loop(status_page: Page, cfg: Config, notify):
    """Раз в DEMANDS_SYNC_S секунд сверяемся со страницей demands: сколько заявок в работе.

    Источник правды о занятых слотах — площадка, а не память: заявку могли взять мы,
    могли выдать автоматически (в памяти её нет вовсе), часть заявок осталась в работе
    с прошлого запуска процесса. Как только заявка перестаёт быть «в работе у
    провайдера» (выполнена или завершилась с ошибкой) — её слот свободен."""
    while state.running and state.mode == "take":
        rows = await _read_demand_rows(status_page, cfg)
        if rows is not None:
            seen_status = {r["id"]: r["status"] for r in rows if r["id"]}
            in_work = _in_work_ids(rows)
            prev = state.demands_ids
            state.demands_ids = in_work

            # Заявки, которые были в работе, а теперь нет — слот освободился.
            for oid in sorted(prev - in_work):
                if oid in state.taken_order_ids:
                    state.taken_order_ids.discard(oid)
                    status = seen_status.get(oid) or "завершена"
                    await notify(f"ℹ️ Заявка #{oid}: статус «{status}» — убираю из очереди, слот свободен.")
                    await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg)}")

            # Заявки, которых мы не брали (автовыдача площадки) — тоже занимают слот.
            auto = sorted(in_work - prev - state.taken_order_ids)
            if auto:
                auto_str = ", ".join(f"#{i}" for i in auto)
                await notify(
                    f"📦 Автовыдача: {auto_str} уже в работе (площадка выдала без нашего взятия)."
                )
                await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg)}")

        for _ in range(max(1, cfg.demands_sync_s)):
            if not (state.running and state.mode == "take"):
                break
            await asyncio.sleep(1)


async def take_loop(choice_page: Page, confirm_page: Page, status_page: Page, cfg: Config, notify, notify_file):
    # Сначала сверяемся с площадкой: сколько заявок уже в работе (включая
    # автовыдачу и оставшиеся с прошлого запуска). Без этого после рестарта
    # процесс набирает лишние заявки сверх TARGET_QUEUE_SIZE.
    await _sync_demands_on_start(status_page, cfg, notify)
    await asyncio.gather(
        fast_take_loop(choice_page, confirm_page, cfg, notify, notify_file),
        status_loop(status_page, cfg, notify),
    )


async def _payout_rows_text(page: Page):
    """Тексты строк таблицы заявок без строки-заглушки «Не найдено»."""
    texts = await page.eval_on_selector_all(
        SEL_PAYOUT_ROWS, "els => els.map(e => e.innerText.trim())"
    )
    return [t for t in texts if t and EMPTY_ROW_TEXT not in t.lower()]


async def watch_loop(page: Page, cfg: Config, notify, notify_file):
    """Режим наблюдения: ничего не кликает, только шлёт уведомление о появлении заявок."""
    await page.goto(cfg.queue_url, wait_until="domcontentloaded")
    await notify("👀 Режим наблюдения запущен. Сообщу, как только появится заявка.")
    prev_count = 0

    while state.running and state.mode == "watch":
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("Reload failed: %s", e)
            await asyncio.sleep(3)
            continue

        if state.snapshot_requested:
            state.snapshot_requested = False
            await _send_live_snapshot(page, cfg, notify, notify_file, "watch")

        if _on_signin(page):
            state.logged_in = False
            state.running = False
            await notify("⚠️ Сессия слетела. Останавливаю наблюдение. Отправь /start_session заново.")
            break

        try:
            rows = await _payout_rows_text(page)
        except Exception as e:
            logger.warning("Rows read failed (страница перезагружалась?): %s", e)
            await asyncio.sleep(1)
            continue

        state.last_status = f"watch: заявок на странице={len(rows)}"

        if len(rows) > prev_count:
            # СНАЧАЛА мгновенно снимаем HTML (заявку могут забрать за секунды),
            # и только потом шлём уведомления.
            ts = int(time.time())
            d = Path(cfg.log_path).parent
            d.mkdir(parents=True, exist_ok=True)
            snap = {}
            try:
                snap["rows"] = await page.eval_on_selector_all(
                    SEL_PAYOUT_ROWS, "els => els.map(e => e.outerHTML).join('\\n\\n')"
                )
            except Exception:
                logger.exception("rows snapshot failed")
            try:
                snap["table"] = await page.eval_on_selector(
                    "table.payout-table", "e => e.outerHTML"
                )
            except Exception:
                logger.exception("table snapshot failed")
            try:
                snap["page"] = await page.content()
            except Exception:
                logger.exception("page snapshot failed")

            files = []
            for key, fname in (("rows", "rows"), ("table", "table"), ("page", "full_page")):
                if snap.get(key):
                    fp = d / f"order_{ts}_{fname}.html"
                    fp.write_text(snap[key], encoding="utf-8")
                    files.append((fp, key))

            preview = "\n".join(f"• {r.replace(chr(10), ' | ')[:150]}" for r in rows[:3])
            await notify(f"🔔 Появилась заявка! Сейчас на странице: {len(rows)}\n{preview}")
            captions = {
                "rows": "HTML строки(строк) заявки — главное для селекторов",
                "table": "HTML всей таблицы заявок",
                "page": "HTML всей страницы целиком",
            }
            for fp, key in files:
                await notify_file(str(fp), captions[key])
        prev_count = len(rows)

        await asyncio.sleep(random.uniform(cfg.reload_min_ms, cfg.reload_max_ms) / 1000)


async def run_worker(cfg: Config, account, ws, notify, notify_file):
    """
    Браузерный воркер кабинета (fallback-бэкенд). Живёт постоянно: ждёт ws.running=True.
    ВНИМАНИЕ: внутренние циклы используют общий state (слой совместимости) и работают
    с кабинетом #1. Для мульти-аккаунта используйте WORKER_BACKEND=http.
    """
    async def acct_notify(text: str):
        await notify(f"[{account.name}] {text}")

    async with async_playwright() as pw:
        browser, context = await _build_context(pw, cfg, account)
        page = await context.new_page()            # логин и режим watch
        confirm_page = await context.new_page()     # быстрая проверка сразу после клика "взять"
        status_page = await context.new_page()       # раз в 30 сек проверяет статусы своей очереди

        while True:
            if not ws.running:
                await asyncio.sleep(1)
                continue

            if not ws.logged_in:
                ok = await ensure_logged_in(page, context, cfg, account, ws, acct_notify)
                if not ok:
                    ws.running = False
                    continue

            if ws.mode == "watch":
                await watch_loop(page, cfg, acct_notify, notify_file)
            else:
                await take_loop(page, confirm_page, status_page, cfg, acct_notify, notify_file)
