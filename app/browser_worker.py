import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

# --- Новый 3-шаговый SPA-вход (Email -> Password -> 2FA) ---
# Отличие от старой серверной формы: шаги сменяют друг друга на одной странице,
# поля размечены id (Vue-компонент), а кнопки — текстом («Далее» / «Войти»).
# Шаг 1: email -> «Далее»; шаг 2: password -> «Войти»; шаг 3: код -> «Войти».
# Селекторы вынесены в Config (см. app/config.py), здесь — значения по умолчанию.
SEL_SPA_EMAIL_INPUT = "input#email"
SEL_SPA_PASSWORD_INPUT = "input#password"
SEL_SPA_2FA_INPUT = "input#twofa-code"
SEL_SPA_NEXT_BUTTON = "button:has-text('Далее')"
SEL_SPA_SUBMIT_BUTTON = "button:has-text('Войти')"

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

# Маркеры, по которым понимаем, что сайт вместо ожидаемой страницы показал
# антибот/капчу/«проверку человека» — это НЕ авторизация, и её нельзя считать успешной.
UNEXPECTED_PAGE_MARKERS = (
    "captcha-delivery.com",
    "datadome",
    "geo.captcha",
    "hcaptcha",
    "recaptcha",
    "captcha",
    "проверка, что вы не робот",
    "вы не робот",
    "verify you are human",
    "are you a robot",
    # Cloudflare-проверка «человек ли вы» перед сайтом (временная, но не является
    # ожидаемой страницей кабинета/входа — по ней надо переждать, см. _wait_*).
    "cf_chl_opt",
    "challenge-platform",
    "challenges.cloudflare.com",
    "один момент",
    "checking your browser",
)


def _looks_like_login_form(html: str) -> bool:
    """Разметка страницы входа: старая форма auth-form ИЛИ новый SPA-вход.

    Старая серверная форма опознаётся по классу auth-form и полям name=email/pass.
    Новый 3-шаговый SPA-вход (Vue) размечен id: #email, #password, #twofa-code —
    класса auth-form там нет, поэтому без этих маркеров страница входа ошибочно
    считалась «неожиданной разметкой» (капчей) и вход не выполнялся.
    """
    low = (html or "").lower()
    markers = (
        "auth-form",
        "name='email'",
        'name="email"',
        "name='pass'",
        'name="pass"',
        # SPA-вход: поля размечены id, класса auth-form нет. Шаги разные, поэтому
        # маркеры для всех трёх полей: страница может открыться сразу на пароле
        # (если email уже введён) или сразу на коде.
        'id="email"',
        "id='email'",
        'id="password"',
        "id='password'",
        'id="twofa-code"',
        "id='twofa-code'",
    )
    return any(m in low for m in markers)


def _looks_like_cabinet(html: str, cfg: Config | None = None) -> bool:
    """Привычная разметка рабочей страницы кабинета (очередь выплат/demands).

    Кроме таблицы выплат (payout-*) сюда входит и стартовая страница кабинета
    «Дашборд» (/account/, класс user-account-page): именно на неё сайт чаще
    всего уводит сразу после входа, а таблицы выплат там нет. Без этого маркера
    успешный вход ошибочно признавался неудачей (дамп debug_after_login_unexpected),
    из-за чего сессия не сохранялась и горячий цикл так и не запускался.
    Маркер user-account-page не встречается на странице входа (там auth-form),
    поэтому ложных срабатываний «мы в кабинете» на форме логина не даёт.

    У SPA (911pay) таблица выплат размечена классом `payouts-compact` — ни одного
    из старых маркеров в ней нет. Без этого маркера УСПЕШНЫЙ SPA-вход считался
    «неожиданной разметкой» (реальный дамп debug_after_login_unexpected_*.html,
    внутри — PayOut - 911pay), сессия не сохранялась и бот шёл на повторный логин.
    Дополнительно учитываются маркеры площадки из `cfg.queue_page_markers`.
    """
    low = (html or "").lower()
    markers = [
        "payout-table",
        "payout-card",
        "payout-status",
        "user-account-page",
        "payouts-compact",
    ]
    for marker in getattr(cfg, "queue_page_markers", ()) or ():
        text = str(marker).lower().strip()
        if text and text not in markers:
            markers.append(text)
    return any(m in low for m in markers)


def _is_challenge_page(html: str) -> bool:
    """Cloudflare-проверка «человек ли вы» перед сайтом (временная, ~20+ сек).

    Это НЕ ошибка и НЕ «сменилась разметка» — это проверка перед /signin и кабинетом.
    Её надо просто переждать, а не признавать неудачей и не снимать дамп.
    """
    low = (html or "").lower()
    markers = (
        "cf_chl_opt",
        "challenge-platform",
        "challenges.cloudflare.com",
        "один момент",
        "<title>один момент",
        "verify you are human",
        "checking your browser",
    )
    return any(m in low for m in markers)


# -------------------------------------------------------------------------
# Клик по Cloudflare Turnstile «Verify you are human».
#
# Интерактивная проверка Cloudflare рендерится виджетом Turnstile внутри
# iframe с домена challenges.cloudflare.com: сам чекбокс — во вложенном
# документе, обычным page.click его не достать. Поэтому кликаем через
# frame_locator (Playwright сам прокинет клик внутрь нужного iframe).
# Всё best-effort: если клик не удался, внешние циклы продолжат ждать и
# попробуют ещё раз на следующей итерации.
# -------------------------------------------------------------------------
TURNSTILE_IFRAME_SELECTORS = (
    # Turnstile-виджет инжектится как iframe с id cf-chl-widget-<token>
    # (рядом на странице есть скрытый input cf-chl-widget-<token>_response).
    "iframe[id^='cf-chl-widget-']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='challenge-platform']",
    "iframe[title*='Cloudflare']",
    "iframe[title*='challenge']",
    "iframe[title*='security']",
)

# Маркеры URL кадра Turnstile. По ним находим нужный iframe среди page.frames —
# надёжнее CSS-селектора и работает на любой вложенности кадров.
TURNSTILE_FRAME_URL_MARKERS = (
    "challenges.cloudflare.com",
    "challenge-platform",
    "cdn-cgi/challenge-platform",
)

# Кандидаты-чекбоксы внутри виджета Turnstile. В разметке managed challenge
# (cType: 'managed') классы рандомизированы, поэтому перебираем варианты,
# включая доступную роль checkbox и контейнер проверки.
TURNSTILE_CHECKBOX_SELECTORS = (
    "input[type='checkbox']",
    ".cb-lb",
    ".ctp-checkbox-label",
    "[role='checkbox']",
    "label",
    "#challenge-stage",
)


async def _try_click(locator, timeout: int = 2000) -> bool:
    """Клик по локатору, если он существует; при перекрытии — force-клик.

    Всё в try/except: локатор может «исчезнуть» при навигации/перерисовке
    кадра. Возвращает True только при успешном клике.
    """
    try:
        if not await locator.count():
            return False
    except Exception:
        return False
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            await locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False


def _is_turnstile_frame_url(url: str) -> bool:
    """Похож ли URL кадра на Turnstile.

    Обычно iframe виджета грузится с домена challenges.cloudflare.com, но иногда
    содержимое рендерится через blob:/about:blank (без домена Cloudflare) — такие
    кадры тоже считаем кандидатами, иначе клик не найдётся.
    """
    u = (url or "").lower()
    if any(m in u for m in TURNSTILE_FRAME_URL_MARKERS):
        return True
    return u.startswith("blob:") or u in ("", "about:blank")


async def _click_in_turnstile_frame(frame) -> bool:
    """Кликает чекбокс внутри одного кадра Turnstile (по роли и по селекторам)."""
    try:
        if await _try_click(frame.get_by_role("checkbox").first):
            return True
    except Exception:
        pass  # у некоторых кадров может не быть get_by_role
    for cb_sel in TURNSTILE_CHECKBOX_SELECTORS:
        try:
            if await _try_click(frame.locator(cb_sel).first):
                return True
        except Exception:
            continue
    return False


async def _click_turnstile(page: Page, notify=None) -> bool:
    """Пробует нажать чекбокс Cloudflare «Verify you are human» (Turnstile).

    Порядок попыток:
      1) перебираем ВСЕ кадры страницы (page.frames, включая вложенные iframe) и
         кликаем в тех, чей URL относится к Cloudflare challenge;
      2) если кадров нет — через frame_locator по селекторам iframe;
      3) managed challenge без iframe — элементы прямо на странице (#challenge-stage).

    Возвращает True, если удалось кликнуть; при неудаче — False, чтобы вызывающий
    цикл попробовал снова (виджет дорисовывается не сразу). Ошибки глушим.
    """
    clicked = False

    # 1) Все кадры страницы (в т.ч. вложенные) — основной путь.
    frames = getattr(page, "frames", None) or []
    for frame in frames:
        if not _is_turnstile_frame_url(getattr(frame, "url", "")):
            continue
        if await _click_in_turnstile_frame(frame):
            clicked = True
            break

    # 2) Через frame_locator (если по кадрам не вышло).
    if not clicked:
        for frame_sel in TURNSTILE_IFRAME_SELECTORS:
            try:
                frame = page.frame_locator(frame_sel)
            except Exception:
                continue
            for cb_sel in TURNSTILE_CHECKBOX_SELECTORS:
                try:
                    if await _try_click(frame.locator(cb_sel).first):
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break

    # 3) Managed challenge без iframe: чекбокс прямо в #challenge-stage.
    if not clicked:
        for cb_sel in (
            "#challenge-stage input[type='checkbox']",
            "#challenge-stage [role='checkbox']",
            "#challenge-stage .ctp-checkbox-label",
            "#challenge-stage",
        ):
            try:
                if await _try_click(page.locator(cb_sel).first):
                    clicked = True
                    break
            except Exception:
                continue

    if clicked and notify is not None:
        await notify("🖱 Прошёл Cloudflare: нажал «Verify you are human».")
    return clicked


async def _query_any(page: Page, selectors: tuple) -> str | None:
    """Первый существующий селектор из списка (None — ни один не найден).

    Нужно, чтобы одна и та же логика работала и на старой серверной форме
    (form.auth-form), и на новом SPA-входе (поля размечены id). Ошибки обращения
    к DOM (страница как раз перезагружается) не пробрасываем — пробуем дальше.
    """
    for sel in selectors:
        try:
            if await page.query_selector(sel):
                return sel
        except Exception:
            continue
    return None


async def _is_visible_any(page: Page, selectors: tuple) -> str | None:
    """Первый ВИДИМЫЙ селектор из списка (None — ни один не виден).

    Важно отличать «есть в разметке» от «видно на экране». SPA-вход держит оба
    шага в DOM одновременно и переключает их через display:none: #password
    существует с самого начала, но скрыт. Проверка по наличию приводила к тому,
    что бот пропускал кнопку «Далее» и падал по таймауту, пытаясь заполнить
    невидимое поле пароля.
    """
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el is None:
                continue
            is_vis = getattr(el, "is_visible", None)
            # Мок-страница в тестах не умеет is_visible() — считаем её видимой,
            # иначе проверка теряет смысл при подменённом DOM.
            if is_vis is None or await is_vis():
                return sel
        except Exception:
            continue
    return None


async def _blur_active(page: Page) -> None:
    """Снимает фокус с активного поля, чтобы SPA пересчитал валидацию.

    На 911pay кнопка «Далее» отрисована сразу, но с атрибутом disabled, и
    включается только после того, как Vue проверит email — а проверка висит на
    blur/change. ``page.fill()`` шлёт лишь input, поэтому кнопка остаётся серой:
    Playwright ждёт actionability (снятия disabled) до таймаута, клик так и не
    происходит, и браузер закрывается. Живой пользователь, кликая по кнопке, сам
    снимает фокус с поля — повторяем это заранее. В мок-странице тестов нет
    evaluate/press — просто пропускаем.
    """
    ev = getattr(page, "evaluate", None)
    if ev is not None:
        try:
            await ev(
                "() => { const a = document.activeElement;"
                " if (a && typeof a.blur === 'function') a.blur(); }"
            )
            return
        except Exception:
            pass
    press = getattr(page, "press", None)
    if press is not None:
        try:
            await press("input:focus", "Tab")
        except Exception:
            pass


async def _click_next_step(page: Page, email_sel: str, next_sel: str) -> None:
    """Жмёт «Далее» на шаге email.

    Кнопка бывает изначально disabled и включается только после валидации email,
    причём валидация запускается по blur/change, а не по input. Поэтому сначала
    снимаем фокус с поля email (``_blur_active``), затем кликаем — с коротким
    таймаутом, чтобы не тратить 15 секунд на заведомо выключенную кнопку.
    Если клик не прошёл — жмём Enter в поле email (кнопка type="submit" внутри
    <form>, Enter отправляет ту же форму). Последняя попытка — снять disabled
    в DOM и кликнуть: Vue мог не включить кнопку сам.
    """
    await _blur_active(page)
    try:
        await page.click(next_sel, timeout=5000)
        return
    except Exception as e:
        logger.info("«Далее» не нажалась (%s) — пробую Enter в поле email", e)
    press = getattr(page, "press", None)
    if press is not None:
        try:
            await press(email_sel, "Enter")
            return
        except Exception as e:
            logger.info("Enter в поле email не сработал (%s)", e)
    ev = getattr(page, "evaluate", None)
    if ev is not None:
        try:
            await ev(
                "(sel) => { const b = document.querySelector(sel);"
                " if (b) { b.removeAttribute('disabled'); b.disabled = false; } }",
                next_sel,
            )
            await page.click(next_sel, timeout=5000)
        except Exception as e:
            logger.warning("«Далее» так и не нажалась даже после снятия disabled: %s", e)


async def _login_form_visible(page: Page) -> bool:
    """Форма входа уже отрисована? Старая серверная ИЛИ новый SPA-шаг с email."""
    return bool(await _query_any(page, (SEL_LOGIN_INPUT, SEL_SPA_EMAIL_INPUT)))


async def _fill_login_credentials(page: Page, cfg: Config, account) -> str:
    """Вводит логин и пароль, возвращая режим входа: "form" (старая форма) или "spa".

    Старая серверная форма: email и пароль отрисованы сразу — заполняем оба поля
    и жмём submit. Новый 3-шаговый SPA-вход (Email -> Password -> 2FA): после
    ввода email поля пароля ещё нет — жмём «Далее», дожидаемся поля пароля,
    заполняем его и жмём «Войти».

    Режим определяется по ФАКТИЧЕСКОЙ разметке (есть ли #email), поэтому код
    одинаково работает и на старой, и на новой площадке — переезд не требует
    правки кода, только env-селекторов при нестандартных id/текстах кнопок.
    """
    spa_email = getattr(cfg, "spa_email_selector", SEL_SPA_EMAIL_INPUT)
    spa_password = getattr(cfg, "spa_password_selector", SEL_SPA_PASSWORD_INPUT)
    spa_next = getattr(cfg, "spa_next_selector", SEL_SPA_NEXT_BUTTON)
    spa_submit = getattr(cfg, "spa_submit_selector", SEL_SPA_SUBMIT_BUTTON)

    if await _query_any(page, (spa_email,)):
        # --- Новый SPA-вход ---
        await page.fill(spa_email, account.login, timeout=10000)
        # Оба шага обычно лежат в разметке одновременно (пароль скрыт через
        # display:none), поэтому смотрим ВИДИМОСТЬ поля пароля, а не наличие:
        # иначе «Далее» пропускается и заполнение падает на скрытом #password.
        if not await _is_visible_any(page, (spa_password,)):
            await _click_next_step(page, spa_email, spa_next)
            try:
                await page.wait_for_selector(spa_password, timeout=15000)
            except Exception:
                # Клик мог уйти раньше, чем Vue включил кнопку: пробуем ещё раз.
                # Но если шаг всё-таки открылся (опоздал на границе таймаута) —
                # повторный клик по скрытой «Далее» только всё сломает.
                if await _is_visible_any(page, (spa_password,)):
                    logger.info("Шаг пароля открылся на границе таймаута — идём дальше")
                else:
                    logger.info("Шаг пароля не открылся — повторяю нажатие «Далее»")
                    await _click_next_step(page, spa_email, spa_next)
                    await page.wait_for_selector(spa_password, timeout=15000)
        await page.fill(spa_password, account.password, timeout=10000)
        # Кнопка «Войти» тоже бывает disabled до валидации пароля по blur/change.
        await _blur_active(page)
        try:
            await page.click(spa_submit, timeout=10000)
        except Exception as e:
            logger.info("«Войти» не нажалась (%s) — пробую Enter в поле пароля", e)
            press = getattr(page, "press", None)
            if press is not None:
                try:
                    await press(spa_password, "Enter")
                except Exception as e2:
                    logger.warning("«Войти» так и не нажалась: %s", e2)
        return "spa"

    # --- Старая серверная форма: email + пароль в одной форме ---
    await page.fill(SEL_LOGIN_INPUT, account.login, timeout=10000)
    await page.fill(SEL_PASSWORD_INPUT, account.password, timeout=10000)
    await page.click(SEL_LOGIN_SUBMIT, timeout=10000)
    return "form"


async def _wait_for_login_form(page: Page, cfg: Config, notify, timeout_s: int) -> bool:
    """Ждём форму входа, пережидая Cloudflare-проверку и редиректы.

    Вместо одного `wait_for_selector` опрашиваем раз в секунду: сайт перед /signin
    нередко гоняет Cloudflare-проверку ~20+ секунд, и ожидание с меньшим таймаутом
    успевает упасть до того, как форма реально появится. Ошибки "execution context
    was destroyed" (страница перезагружается) — норма, пропускаем их.
    """
    notified_challenge = False
    clicked_challenge = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # 1. Форма уже есть? (старая серверная ИЛИ новый SPA-шаг с email)
        if await _login_form_visible(page):
            return True

        # 2. Не Cloudflare ли сейчас? (это не ошибка, а пауза — ждём дальше)
        try:
            html = await page.content()
            if _is_challenge_page(html):
                if not notified_challenge:
                    notified_challenge = True
                    await notify(
                        "⏳ Сайт показывает Cloudflare-проверку «человек ли вы» перед входом. "
                        f"Жду, пока пройдёт (до {timeout_s} сек)…"
                    )
                # Пробуем сами нажать «Verify you are human», пока проверка не пройдёт.
                if not clicked_challenge:
                    clicked_challenge = await _click_turnstile(page, notify)
        except Exception:
            pass  # контекст пересоздаётся — пропускаем

        await asyncio.sleep(1)

    # Последняя проверка перед тем, как признать неудачу: форма могла появиться
    # в момент, когда мы уже вышли из while.
    return await _login_form_visible(page)


async def _wait_challenge_passed(page: Page, cfg: Config, notify, timeout_s: int = 90) -> None:
    """Если после goto открылась Cloudflare-проверка, дожидаемся её окончания.

    Возвращаемся только когда проверка прошла (или истёк таймаут). Тогда сайт
    редиректит либо в кабинет, либо на /signin — и по нему уже можно решать.
    """
    notified = False
    clicked = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            html = await page.content()
        except Exception:
            try:
                await asyncio.sleep(1)
                continue
            except Exception:
                continue
        if not _is_challenge_page(html):
            return
        if not notified:
            notified = True
            await notify(
                "⏳ Сайт показывает Cloudflare-проверку. Дожидаюсь, пока она пройдёт…"
            )
        # Пробуем сами нажать «Verify you are human», пока проверка не пройдёт.
        if not clicked:
            clicked = await _click_turnstile(page, notify)
        await asyncio.sleep(1)


async def _wait_url_settle(page: Page, max_wait_ms: int = 15000) -> str:
    """Ждём, пока URL перестанет меняться (SPA, редиректы после логина).

    Старая версия верила странице сразу после domcontentloaded — из-за медленных
    редиректов бот «не видел» /signin и ошибочно писал «сессия активна».
    """
    deadline = time.monotonic() + max_wait_ms / 1000
    last_url = page.url
    stable_ticks = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if page.url != last_url:
            last_url = page.url
            stable_ticks = 0
        else:
            stable_ticks += 1
            if stable_ticks >= 3:  # ~0.6 с без смены URL
                break
    return page.url


async def _send_debug_dump(page: Page, cfg: Config, notify, notify_file, name: str, caption: str) -> str:
    """Скриншот + HTML текущей страницы: сохраняет в ./data и отправляет оба файла админу.

    Нужно, чтобы при изменившейся разметке/капче можно было посмотреть в Telegram,
    что именно бот увидел вместо ожидаемой страницы.
    """
    d = Path(cfg.log_path).parent
    d.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    png_path = d / f"debug_{name}_{ts}.png"
    html_path = d / f"debug_{name}_{ts}.html"
    page_url = page.url
    try:
        await page.screenshot(path=str(png_path), full_page=True)
    except Exception:
        logger.exception("screenshot в debug-дампе не удался")
    try:
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception:
        logger.exception("html в debug-дампе не удался")
        html = ""
    # Файлы сохраняем в ./data всегда (пригодятся для разбора), а в Telegram шлём
    # только при DEBUG=true — иначе бот молча пишет их на диск и не спамит.
    if not cfg.debug:
        logger.info("debug-дамп сохранён локально (DEBUG=false): %s, %s", html_path, png_path)
        return html

    await notify(f"{caption}\nURL: {page_url}\nФайлы: {html_path.name}, {png_path.name}")
    if notify_file is not None:
        try:
            await notify_file(str(html_path), f"HTML [{name}] — для анализа разметки")
        except Exception:
            logger.exception("не удалось отправить HTML-дамп в Telegram")
        try:
            await notify_file(str(png_path), f"Скриншот [{name}]")
        except Exception:
            logger.exception("не удалось отправить скриншот в Telegram")
    return html


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


def _playwright_proxy(url: str | None) -> dict | None:
    """URL прокси → формат Playwright: {"server", "username", "password"}."""
    if not url:
        return None
    p = urlsplit(url)
    if not p.hostname:
        return None
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    out = {"server": server}
    if p.username:
        out["username"] = unquote(p.username)
    if p.password:
        out["password"] = unquote(p.password)
    return out


async def _build_context(pw, cfg: Config, account, proxy: str | None = None) -> BrowserContext:
    browser = await pw.chromium.launch(
        headless=cfg.headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    storage_path = Path(account.storage_state_path)
    kwargs = {}
    pw_proxy = _playwright_proxy(proxy)
    if pw_proxy:
        kwargs["proxy"] = pw_proxy
    if storage_path.exists():
        context = await browser.new_context(storage_state=str(storage_path), **kwargs)
    else:
        context = await browser.new_context(**kwargs)
    return browser, context


def _on_signin(page: Page, cfg: Config | None = None) -> bool:
    """Мы всё ещё на странице входа?

    Путь берётся из конфига площадки (`SIGNIN_PATH`): у 911pay это `/login`, у
    прежней площадки — `/signin`. Жёсткий `/signin` ломал ожидание после пароля
    (URL `/login` считался «ушли со входа»), из-за чего бот не дожидался
    авторизации и мог сохранить нерабочую сессию.
    """
    path = getattr(cfg, "signin_path", None) or "/signin"
    return path in page.url


async def ensure_logged_in(
    page: Page,
    context: BrowserContext,
    cfg: Config,
    account,
    ws,
    notify,
    notify_file=None,
) -> bool:
    # Пробуем открыть рабочую страницу: если сессия жива, редиректа на /signin не будет.
    # ВАЖНО: не верим первому URL — ждём, пока адрес перестанет меняться (медленный
    # редирект на /signin стоит сайте пол-секунды, и мы обязаны его дождаться).
    login_timeout = getattr(cfg, "login_form_timeout_s", 90)
    await page.goto(cfg.queue_url, wait_until="domcontentloaded")
    await _wait_url_settle(page)
    if not _on_signin(page, cfg):
        # Вне /signin — но точно ли мы в кабинете? Сайт перед /signin может сначала
        # показать Cloudflare-проверку «Один момент…» и кратковременную страницу
        # редиректа/загрузки, а форму входа «дорисовать» чуть позже. Поэтому читать
        # разметку один раз нельзя — на мгновенной «заглушке» получится ложный дамп
        # unexpected_after_queue, хотя на деле открывалась обычная форма входа
        # (см. debug_unexpected_after_queue_*.html с формой «Авторизация»).
        # Ждём, пока страница не превратится в кабинет, форму входа, URL /signin
        # или (по истечении таймаута) в действительно неожиданную разметку.
        deadline = time.monotonic() + login_timeout
        resolved = False
        while time.monotonic() < deadline and not resolved:
            try:
                html = await page.content()
            except Exception:
                html = ""
            if _looks_like_cabinet(html, cfg):
                ws.logged_in = True
                await notify("✅ Сессия активна, повторная авторизация не нужна.")
                return True
            if _looks_like_login_form(html):
                resolved = True          # открылась форма входа — идём в flow логина
                break
            if _is_challenge_page(html):
                # Cloudflare-проверку пережидаем, потом заново оцениваем страницу.
                await _wait_challenge_passed(page, cfg, notify, login_timeout)
                continue
            if _on_signin(page, cfg):     # редирект на страницу входа — там форма
                resolved = True
                break
            await asyncio.sleep(1)
        if not resolved:
            # Таймаут: ни кабинета, ни формы так и не появилось. Пере-проверяем
            # один раз (форма могла «доехать» ровно при выходе из цикла) и только
            # тогда признаём неудачу и шлём дамп.
            try:
                html = await page.content()
            except Exception:
                html = ""
            if _looks_like_cabinet(html, cfg):
                ws.logged_in = True
                await notify("✅ Сессия активна, повторная авторизация не нужна.")
                return True
            if _on_signin(page, cfg) or _looks_like_login_form(html):
                resolved = True
            else:
                await _send_debug_dump(
                    page,
                    cfg,
                    notify,
                    notify_file,
                    "unexpected_after_queue",
                    "⚠️ После открытия рабочего URL сайт показал неожиданную разметку "
                    "(не кабинет и не форма входа) — возможно капча/антибот или сайт изменился. "
                    "Присылаю HTML и скриншот. Сессия НЕ считается успешной.",
                )
                return False

    await notify("🔐 Начинаю авторизацию на сайте...")
    await page.goto(cfg.site_url, wait_until="domcontentloaded")
    # Дожидаемся именно формы входа, пережидая Cloudflare-проверку и редиректы.
    # Если сайт стал грузиться дольше или разметка сменилась — только тогда дамп.
    if not await _wait_for_login_form(page, cfg, notify, login_timeout):
        await _send_debug_dump(
            page,
            cfg,
            notify,
            notify_file,
            "login_form_missing",
            f"⚠️ Форма входа не появилась за {login_timeout} секунд — страница "
            "авторизации не догрузилась или изменилась. Присылаю HTML и скриншот для разбора.",
        )
        return False

    # Для диагностики всегда присылаем HTML/скриншот страницы авторизации.
    await _send_debug_dump(
        page,
        cfg,
        notify,
        notify_file,
        "login_page_dump",
        "🔐 Страница авторизации (HTML-дамп для проверки).",
    )
    try:
        # Старая серверная форма и новый 3-шаговый SPA-вход обслуживаются одной
        # функцией: режим определяется по фактической разметке (есть ли #email).
        login_mode = await _fill_login_credentials(page, cfg, account)
    except Exception as e:
        await _send_debug_dump(
            page,
            cfg,
            notify,
            notify_file,
            "login_form",
            f"⚠️ Не удалось заполнить форму входа: {e}",
        )
        return False

    # Ждём: либо уйдём со /signin (без 2FA), либо появится поле кода.
    # Для SPA поле 2FA проверяем ДО URL: пошаговый вход может остаться на том же
    # адресе (или сменить роут уже без /signin) и при этом показывать шаг с кодом —
    # иначе 2FA пропускался и вход обрывался.
    code_input = None
    twofa_input_sel = (
        getattr(cfg, "spa_twofa_input_selector", SEL_SPA_2FA_INPUT)
        if login_mode == "spa"
        else SEL_2FA_INPUT
    )
    for _ in range(40):  # ~20 сек
        await page.wait_for_timeout(500)
        try:
            if await page.query_selector(twofa_input_sel):
                code_input = True
                break
        except Exception:
            pass  # страница как раз перезагружается — проверим URL и повторим
        if not _on_signin(page, cfg):
            break
    else:
        # Всё ещё на /signin и кода нет — вероятно, неправильный пароль или ошибка на форме.
        await _send_debug_dump(
            page,
            cfg,
            notify,
            notify_file,
            "after_login_still_signin",
            "⚠️ После отправки логина/пароля мы так и остались на странице входа "
            "(кода 2FA тоже нет). Скорее всего неверный пароль или сайт показал ошибку. "
            "Присылаю HTML и скриншот.",
        )
        return False

    if code_input:
        await notify("📲 Введите код 2FA ответным сообщением в этот чат (5 минут).")
        try:
            code = await ws.wait_for_2fa_code(timeout=300)
        except asyncio.TimeoutError:
            await notify("⏱ Код не получен за 5 минут. Отправьте /start_session заново.")
            return False
        try:
            await page.fill(twofa_input_sel, code, timeout=10000)
        except Exception as e:
            await _send_debug_dump(
                page,
                cfg,
                notify,
                notify_file,
                "2fa_form",
                f"⚠️ Ошибка при вводе 2FA: {e}",
            )
            return False

        # Сайт может сам отправить форму сразу после ввода кода ("Проверка кода...")
        # и уйти на другую страницу — тогда клик не нужен, а обращение к DOM
        # падает с "Execution context was destroyed". Это нормально, игнорируем.
        twofa_submit_sel = (
            getattr(cfg, "spa_submit_selector", SEL_SPA_SUBMIT_BUTTON)
            if login_mode == "spa"
            else SEL_2FA_SUBMIT
        )
        try:
            await page.click(twofa_submit_sel, timeout=3000)
        except Exception as e:
            logger.info("2FA submit click skipped (форма уже отправлена?): %s", e)

        try:
            signin_path = getattr(cfg, "signin_path", None) or "/signin"
            await page.wait_for_url(lambda u: signin_path not in u, timeout=15000)
        except Exception:
            pass

    # Если сайт остался на /signin, хотя код принят — сессия обычно уже создана.
    # Принудительно открываем рабочую страницу, ждём стабилизации и проверяем.
    if _on_signin(page, cfg):
        await page.goto(cfg.queue_url, wait_until="domcontentloaded")
        await _wait_url_settle(page)

    if _on_signin(page, cfg):
        await _send_debug_dump(
            page,
            cfg,
            notify,
            notify_file,
            "after_login",
            "❌ Всё ещё на странице входа: неверный пароль/код или другие селекторы. "
            "Присылаю HTML и скриншот.",
        )
        return False

    # Финальная страховка: ушли с /signin, но точно ли открылся кабинет, а не капча?
    async def _page_html() -> str:
        try:
            return await page.content()
        except Exception:
            return ""

    html = await _page_html()
    # После входа сайт нередко отдаёт Cloudflare-проверку «Один момент…» — это НЕ
    # провал авторизации: сессия уже создана, нужно лишь переждать проверку (и
    # нажать Turnstile), затем перепроверить страницу. Без этого успешный вход
    # помечался как after_login_unexpected (дамп с challenge-разметкой «Один
    # момент…»), сессия не сохранялась и горячий цикл так и не запускался.
    if _is_challenge_page(html):
        await _wait_challenge_passed(page, cfg, notify, login_timeout)
        if _on_signin(page, cfg):
            await _send_debug_dump(
                page,
                cfg,
                notify,
                notify_file,
                "after_login",
                "❌ После Cloudflare-проверки сайт снова на странице входа — "
                "авторизация не установлена. Присылаю HTML и скриншот.",
            )
            return False
        html = await _page_html()
    if not _looks_like_cabinet(html, cfg) and not _looks_like_login_form(html):
        await _send_debug_dump(
            page,
            cfg,
            notify,
            notify_file,
            "after_login_unexpected",
            "⚠️ Ушли со страницы входа, но разметка не похожа на кабинет — вероятно "
            "капча/антибот/промежуточная страница. Сессия НЕ считается успешной. "
            "Присылаю HTML и скриншот.",
        )
        return False

    Path(account.storage_state_path).parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=account.storage_state_path)
    ws.logged_in = True
    await notify("✅ Авторизация успешна, сессия сохранена.")
    return True


async def login_via_browser(cfg: Config, account, ws, notify, notify_file=None, proxy: str | None = None) -> bool:
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
        return await _login_via_browser_locked(cfg, account, ws, notify, notify_file, proxy=proxy)


async def _login_via_browser_locked(
    cfg: Config, account, ws, notify, notify_file=None, proxy: str | None = None
) -> bool:
    """Сама авторизация; вызывать только под _login_lock()."""
    async with async_playwright() as pw:
        browser, context = await _build_context(pw, cfg, account, proxy=proxy)
        try:
            page = await context.new_page()
            return await ensure_logged_in(page, context, cfg, account, ws, notify, notify_file)
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
    if cfg.debug:
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
        if cfg.debug:
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
        if state.resync_required:
            # /reset_session очистил кэш: ждём, пока status_loop сделает свежую
            # сверку с площадкой. На «пустом» кэше брать нельзя — наберём лишнее.
            await notify("🔄 /reset_session: жду свежую сверку с площадкой…")
            while state.resync_required and state.running and state.mode == "take":
                await asyncio.sleep(0.5)
            continue
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

        if _on_signin(choice_page, cfg):
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
        # /reset_session попросил пересчитать очередь немедленно (не ждём
        # DEMANDS_SYNC_S). После успешной сверки флаг снимается, и только тогда
        # fast_take_loop продолжит брать заявки.
        if state.resync_required:
            try:
                while state.resync_required and state.running and state.mode == "take":
                    rows = await _read_demand_rows(status_page, cfg)
                    if rows is None:
                        for _ in range(max(1, cfg.demands_sync_s)):
                            if not state.resync_required:
                                break
                            await asyncio.sleep(1)
                        continue
                    state.taken_order_ids.intersection_update(_in_work_ids(rows))
                    # Сверка пересоздаёт demands_ids целиком: старые «мёртвые» id
                    # из памяти выпадут автоматически.
                    state.demands_ids = _in_work_ids(rows)
                    state.resync_required = False
                    await notify(f"🔄 /reset_session: очередь пересчитана по площадке. {_queue_snapshot_text(cfg)}")
            except Exception:
                logger.exception("Ошибка resync после /reset_session")
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
                    if cfg.debug:
                        await notify(f"🔄 Очередь изменилась. {_queue_snapshot_text(cfg)}")

            # Заявки, которых мы не брали (автовыдача площадки) — тоже занимают слот.
            auto = sorted(in_work - prev - state.taken_order_ids)
            if auto:
                auto_str = ", ".join(f"#{i}" for i in auto)
                await notify(
                    f"📦 Автовыдача: {auto_str} уже в работе (площадка выдала без нашего взятия)."
                )
                if cfg.debug:
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

        if _on_signin(page, cfg):
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

    async def acct_notify_file(path: str, caption: str = ""):
        await notify_file(path, f"[{account.name}] {caption}".strip())

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
                ok = await ensure_logged_in(page, context, cfg, account, ws, acct_notify, acct_notify_file)
                if not ok:
                    ws.running = False
                    continue

            if ws.mode == "watch":
                await watch_loop(page, cfg, acct_notify, notify_file)
            else:
                await take_loop(page, confirm_page, status_page, cfg, acct_notify, notify_file)
