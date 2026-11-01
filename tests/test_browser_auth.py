"""Тесты логики авторизации browser_worker без реального браузера.

ensure_logged_in гоняем на фейковых Page/BrowserContext (только async-методы,
которые использует авторизация), поэтому тесты работают офлайн и быстро.
Проверяем главное, ради чего менялась логика:
  • сессия «жива» только когда страница похожа на кабинет (не по одному URL);
  • при неожиданной разметке / капче после queue_url отправляется HTML-дамп;
  • при отсутствии формы входа отправляется дамп, а не тихий «успех»;
  • после логина финальная проверка разметки: ушли со /signin, но попали
    на капчу — сессия НЕ сохраняется, дамп уходит в Telegram.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.browser_worker import (
    SEL_2FA_INPUT,
    SEL_2FA_SUBMIT,
    SEL_LOGIN_INPUT,
    SEL_LOGIN_SUBMIT,
    SEL_SPA_2FA_INPUT,
    SEL_SPA_EMAIL_INPUT,
    SEL_SPA_NEXT_BUTTON,
    SEL_SPA_PASSWORD_INPUT,
    SEL_SPA_SUBMIT_BUTTON,
    _blur_active,
    _click_next_step,
    _click_turnstile,
    _fill_login_credentials,
    _is_challenge_page,
    _is_turnstile_frame_url,
    _looks_like_cabinet,
    _looks_like_login_form,
    _send_debug_dump,
    ensure_logged_in,
)
from app.state import WorkerState

QUEUE_URL = "https://x/account/payouts/?sub=payouts_choice"
SITE_URL = "https://x/signin"
LOG_PATH = "/tmp/auth_test_actions.log"
STORAGE_PATH = "/tmp/auth_test_state.json"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

LOGIN_HTML = (
    "<html><body><form class='auth-form'>"
    "<input name='email'><input name='pass'>"
    "<button class='auth-submit'>Войти</button></form></body></html>"
)
TWOFA_HTML = (
    "<html><body><form id='form_tg'><input name='code'>"
    "<button class='auth-submit'>OK</button></form></body></html>"
)
# Новый 3-шаговый SPA-вход (Vue): поля размечены id, класса auth-form нет,
# кнопки — текстом («Далее» / «Войти»). Шаг 1 — только email.
SPA_LOGIN_HTML = (
    "<html><body><div class='w-full sm:max-w-md'>"
    "<form><input id='email' name='email' type='email' placeholder='Ваша почта'>"
    "<button type='submit'><span>Далее</span></button></form></div></body></html>"
)
# Шаг 2 — пароль и «Запомнить меня» (на одной странице с email бывает только
# у старой серверной формы; здесь отдельный шаг).
SPA_PASSWORD_HTML = (
    "<html><body><div class='w-full sm:max-w-md'>"
    "<form><input id='password' name='password' type='password'>"
    "<label><input type='checkbox' name='remember'>Запомнить меня</label>"
    "<button type='submit'><span>Войти</span></button></form></div></body></html>"
)
# Шаг 3 — код 2FA.
SPA_TWOFA_HTML = (
    "<html><body><div class='w-full sm:max-w-md'>"
    "<form><input id='twofa-code' name='code' type='text' placeholder='000000'>"
    "<button type='submit'><span>Войти</span></button></form></div></body></html>"
)
CABINET_HTML = (
    "<html><body><div class='payout-card'>"
    "<table class='payout-table'><tbody><tr><td class='payout-status'>"
    "<b># 1</b></td></tr></tbody></table></div></body></html>"
)
# Стартовая страница кабинета «Дашборд» (/account/): сайт уводит туда сразу
# после входа. Таблицы выплат (payout-*) здесь нет, есть только user-account-page.
DASHBOARD_HTML = (
    "<html><body><div class='container-fluid user-account-page'>"
    "<div class='card-stat'><div class='card-stat__label'>Ваш депозит</div>"
    "<div class='card-stat__value'>0.00 USD</div></div>"
    "</div></body></html>"
)
CAPTCHA_HTML = (
    "<html><body><div>Security check</div>"
    "<script src='https://geo.captcha-delivery.com/c.js'></script>"
    "</body></html>"
)
CHALLENGE_HTML = (
    "<!DOCTYPE html><html lang='ru-ru'><head><title>Один момент…</title></head>"
    "<body><div class='challenge-container' id='cf-chl-running'></div>"
    "<script src='https://challenges.cloudflare.com/turnstile/api.js'></script>"
    "</body></html>"
)


class Collector:
    """notify/notify_file — собирает тексты и отправленные файлы."""

    def __init__(self):
        self.messages = []
        self.files = []

    async def notify(self, text: str):
        self.messages.append(text)

    async def notify_file(self, path: str, caption: str = ""):
        self.files.append((path, caption))


class _VisibilityStub:
    """Элемент с is_visible(), как у настоящего Playwright.

    Нужен, чтобы отличить «поля нет в разметке» от «поле есть, но скрыто».
    Второй случай — реальная разметка SPA: оба шага отрисованы сразу, неактивный
    спрятан через display:none. Раньше проверка по наличию в DOM считала такой
    шаг активным, бот пропускал «Далее» и падал на заполнении скрытого поля.
    """

    def __init__(self, visible_fn):
        self._visible_fn = visible_fn

    async def is_visible(self) -> bool:
        return bool(self._visible_fn())


class FakePage:
    """Минимальный Playwright-Page для ensure_logged_in (только используемые методы)."""

    def __init__(self):
        self._url = ""
        self._html = ""
        # Поведение, задаваемое тестом:
        self.goto_handler = None           # async (url) -> None (меняет _url/_html)
        self.after_login_handler = None    # async () -> None (после submit логина)
        self.after_2fa_handler = None      # async () -> None (после submit 2FA)
        self.content_hook = None           # async () -> str (динамическая разметка)
        self.login_visible_hook = None     # async () -> bool (когда форма "видна")
        self.form_present = True
        self.show_2fa_after_login = False
        self.form_timeout = False          # форма так и не появляется
        # Новый 3-шаговый SPA-вход: email -> «Далее» -> password -> «Войти».
        self.spa_login = False
        self.spa_password_visible = False
        # Реальная разметка 911pay: ОБА шага уже в DOM, пароль скрыт через
        # display:none. Наличие поля в разметке НЕ значит, что шаг активен.
        self.spa_password_in_dom = False
        self.goto_log = []
        self.fill_log = []
        self.click_log = []
        self.screenshot_calls = 0

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url, **kwargs):
        self.goto_log.append(url)
        if self.goto_handler:
            await self.goto_handler(url)
        else:
            self._url = url

    async def wait_for_timeout(self, ms: int):
        return None

    async def content(self) -> str:
        if self.content_hook:
            return await self.content_hook()
        return self._html

    async def screenshot(self, **kwargs):
        self.screenshot_calls += 1
        return None

    async def wait_for_selector(self, selector, timeout=None):
        # Старая серверная форма.
        if selector == SEL_LOGIN_INPUT and not self.spa_login:
            if self.form_timeout or not self.form_present:
                raise TimeoutError("login form not found")
            return SimpleNamespace()
        # Новый SPA-вход: шаг email и шаг пароля.
        if self.spa_login and selector == SEL_SPA_EMAIL_INPUT:
            if self.form_timeout or not self.form_present:
                raise TimeoutError("spa login form not found")
            return SimpleNamespace()
        if self.spa_login and selector == SEL_SPA_PASSWORD_INPUT:
            if not self.spa_password_visible:
                raise TimeoutError("spa password step not shown")
            return SimpleNamespace()
        if self.show_2fa_after_login and selector in (SEL_2FA_INPUT, SEL_SPA_2FA_INPUT):
            return SimpleNamespace()
        raise TimeoutError(f"selector {selector} not found")

    async def query_selector(self, selector):
        if self.show_2fa_after_login and selector in (SEL_2FA_INPUT, SEL_SPA_2FA_INPUT):
            return SimpleNamespace()
        # Новый SPA-вход: email виден сразу, пароль — только после «Далее».
        if self.spa_login and selector == SEL_SPA_EMAIL_INPUT:
            if self.form_present and not self.form_timeout:
                return SimpleNamespace()
            return None
        if self.spa_login and selector == SEL_SPA_PASSWORD_INPUT:
            # Реальная разметка 911pay: поле пароля отрисовано сразу, но скрыто
            # через display:none до нажатия «Далее». Флаг spa_password_in_dom
            # включает этот режим — элемент ЕСТЬ, но is_visible() = False.
            if self.spa_password_in_dom:
                return _VisibilityStub(lambda: self.spa_password_visible)
            if self.spa_password_visible:
                return SimpleNamespace()
        # Старая серверная форма.
        if selector == SEL_LOGIN_INPUT and not self.spa_login:
            if self.login_visible_hook:
                return SimpleNamespace() if await self.login_visible_hook() else None
            if self.form_present and not self.form_timeout:
                return SimpleNamespace()
            return None
        return None

    async def fill(self, selector, value, timeout=None):
        self.fill_log.append((selector, value))
        return None

    async def click(self, selector, timeout=None):
        self.click_log.append(selector)
        # Новый SPA: «Далее» открывает шаг пароля.
        if selector == SEL_SPA_NEXT_BUTTON and self.spa_login:
            self.spa_password_visible = True
            return None
        # Новый SPA: «Войти» — submit шага пароля ИЛИ шага кода. Какой именно,
        # понятно по флагу show_2fa_after_login: после шага пароля тест включает
        # его, и следующий клик «Войти» отправляет код.
        if selector == SEL_SPA_SUBMIT_BUTTON:
            if self.show_2fa_after_login:
                if self.after_2fa_handler:
                    await self.after_2fa_handler()
            elif self.after_login_handler:
                await self.after_login_handler()
            return None
        if selector == SEL_LOGIN_SUBMIT and self.after_login_handler:
            await self.after_login_handler()
        if selector == SEL_2FA_SUBMIT and self.after_2fa_handler:
            await self.after_2fa_handler()
        return None

    async def wait_for_url(self, predicate, timeout=None):
        if not predicate(self.url):
            raise TimeoutError("url not changed")
        return None


class FakeContext:
    def __init__(self):
        self.storage_calls = []

    async def storage_state(self, path=None):
        self.storage_calls.append(path)


def make_world(**kw):
    page = FakePage()
    context = FakeContext()
    collector = Collector()
    cfg = SimpleNamespace(
        queue_url=QUEUE_URL,
        site_url=SITE_URL,
        log_path=LOG_PATH,
        demands_url="https://x/account/payouts/?sub=demands",
        # маленький таймаут ожидания формы, чтобы тесты шли быстро (по умолчанию в
        # реальном Config он 90 сек, но в юнит-тестах ждать не нужно).
        login_form_timeout_s=kw.pop("login_form_timeout_s", 2),
        # отладочные дампы в Telegram включены (как в проде по умолчанию)
        debug=kw.pop("debug", True),
    )
    account = SimpleNamespace(
        index=1,
        name="Кабинет 1",
        login="a@b.c",
        password="p",
        storage_state_path=STORAGE_PATH,
    )
    ws = WorkerState(index=1, name="Кабинет 1")
    return page, context, cfg, account, ws, collector


async def _instant_sleep(*_args, **_kwargs):
    """Заменяет asyncio.sleep в browser_worker, чтобы тесты не ждали реальное время
    в циклах _wait_for_login_form / _wait_challenge_passed / _wait_url_settle."""
    return None

def make_challenge_then(page, challenge_polls: int = 3):
    """FakePage-хуки «разметка меняется со временем»: первые challenge_polls
    обращений к content() показывают Cloudflare-проверку, дальше — page._html
    (то, что сайт отдал на самом деле: прибор/кабинет). Возвращает пару
    (content_hook, login_visible_hook) для установки на FakePage.
    """
    state = {"calls": 0}

    async def content_hook():
        state["calls"] += 1
        if state["calls"] <= challenge_polls:
            return CHALLENGE_HTML
        return page._html

    async def login_visible_hook():
        # форма входа «появляется» только после того, как проверка закончилась
        return state["calls"] > challenge_polls

    return content_hook, login_visible_hook

class PageLooksTest(unittest.TestCase):
    def test_login_form_detected_from_auth_form(self):
        self.assertTrue(_looks_like_login_form(LOGIN_HTML))
        self.assertTrue(_looks_like_login_form("<input name='pass'>"))

    def test_spa_login_form_detected_by_id_markers(self):
        """Новый 3-шаговый SPA-вход размечен id и не имеет класса auth-form."""
        self.assertTrue(_looks_like_login_form(SPA_LOGIN_HTML))
        self.assertTrue(_looks_like_login_form(SPA_PASSWORD_HTML))
        self.assertTrue(_looks_like_login_form(SPA_TWOFA_HTML))

    def test_cabinet_detected_from_payout_rows(self):
        self.assertTrue(_looks_like_cabinet(CABINET_HTML))

    def test_cabinet_detected_from_dashboard_landing(self):
        """Дашборд /account/ (user-account-page) — тоже валидный «мы вошли»."""
        self.assertTrue(_looks_like_cabinet(DASHBOARD_HTML))
        # …и при этом не путается с формой входа.
        self.assertFalse(_looks_like_login_form(DASHBOARD_HTML))

    def test_spa_payouts_compact_counts_as_cabinet(self):
        """Реальная SPA-таблица 911pay: <table class="w-full payouts-compact">.

        Старые маркеры (payout-table/payout-card/payout-status) в ней не
        встречаются, поэтому успешный вход ошибочно считался «неожиданной
        разметкой» (дамп debug_after_login_unexpected_*, внутри PayOut - 911pay).
        """
        html = '<div id="app"><table class="w-full payouts-compact"><tbody></tbody></table></div>'
        self.assertTrue(_looks_like_cabinet(html))
        self.assertFalse(_looks_like_login_form(html))

    def test_platform_queue_markers_from_config(self):
        """Маркеры площадки из QUEUE_PAGE_MARKERS тоже означают «мы в кабинете»."""
        cfg = SimpleNamespace(queue_page_markers=("custom-queue",))
        self.assertTrue(_looks_like_cabinet("<div class='custom-queue'>", cfg))

    def test_captcha_is_neither_cabinet_nor_login_form(self):
        self.assertFalse(_looks_like_cabinet(CAPTCHA_HTML))
        self.assertFalse(_looks_like_login_form(CAPTCHA_HTML))

    def test_challenge_page_detected(self):
        self.assertTrue(_is_challenge_page(CHALLENGE_HTML))
        self.assertFalse(_is_challenge_page(LOGIN_HTML))
        self.assertFalse(_is_challenge_page(CABINET_HTML))

    # Реальные дампы «сайт отдал проверку вместо кабинета». Оба имени встречаются:
    # старое debug_unexpected_after_queue_* (на queue_url) и новое
    # debug_after_login_unexpected_* (после успешного ввода логина/пароля сайт
    # показал Cloudflare «Один момент…»).
    _REAL_CHALLENGE_DUMPS = (
        "debug_unexpected_after_queue*.html",
        "debug_after_login_unexpected*.html",
    )

    @unittest.skipUnless(
        any(DATA_DIR.glob(p) for p in _REAL_CHALLENGE_DUMPS),
        "нет реального дампа Cloudflare-проверки в ./data",
    )
    def test_challenge_recognized_in_real_dump(self):
        # Старые дампы могли быть написаны багованной версией, которая помечала
        # обычную форму входа как «неожиданная разметка» (логин-форма там, а не
        # Cloudflare). Проверяем распознавание только на настоящих дампах проверки.
        seen = 0
        for pattern in self._REAL_CHALLENGE_DUMPS:
            for fp in sorted(DATA_DIR.glob(pattern)):
                html = fp.read_text(encoding="utf-8")
                if _looks_like_login_form(html) or _looks_like_cabinet(html):
                    continue
                self.assertTrue(_is_challenge_page(html), fp.name)
                seen += 1
        self.assertTrue(seen, "не нашлось ни одного реального дампа проверки")


class EnsureLoggedInTest(unittest.TestCase):
    def tearDown(self):
        # FIXME: сбрасываем общий WorkerState из модульных тестов
        pass

    def test_active_session_ok_when_page_looks_like_cabinet(self):
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            if url == QUEUE_URL:
                page._url = QUEUE_URL
                page._html = CABINET_HTML

        page.goto_handler = goto_handler
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertIn("Сессия активна", "\n".join(col.messages))
        self.assertEqual(col.files, [])  # успех — дампов не шлём

    def test_unexpected_markup_after_queue_sends_dump(self):
        """Вместо кабинета сайт отдал капчу — шлём дамп и не считаем сессию успешной."""
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            if url == QUEUE_URL:
                page._url = QUEUE_URL  # URL без /signin, но разметка — капча
                page._html = CAPTCHA_HTML
            else:
                page._url = SITE_URL
                page._html = LOGIN_HTML

        page.goto_handler = goto_handler
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertIn("неожиданную разметку", "\n".join(col.messages))
        self.assertTrue(col.files, "должен был уйти HTML-дамп в Telegram")
        self.assertTrue(any("HTML" in caption for _, caption in col.files))

    def test_cloudflare_longer_than_old_timeout_then_login_is_success(self):
        """Cloudflare-проверка длиннее старых 20 c, затем форма — вход успешен,
        дампа login_form_missing нет (бот ждёт до LOGIN_FORM_TIMEOUT_S)."""
        page, context, cfg, account, ws, col = make_world(login_form_timeout_s=30)
        page._html = LOGIN_HTML  # реальная форма (откроется ПОСЛЕ проверки)

        async def goto_handler(url):
            # и queue, и site ведут на /signin, где сначала идёт Cloudflare
            page._url = SITE_URL
            page._html = LOGIN_HTML

        async def after_login():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        # Cloudflare «дольше старых 20 c», но укладывается в 30-секундный таймаут
        page.content_hook, page.login_visible_hook = make_challenge_then(page, challenge_polls=25)
        page.goto_handler = goto_handler
        page.after_login_handler = after_login

        with mock.patch("asyncio.sleep", _instant_sleep):
            ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        joined = "\n".join(col.messages)
        self.assertNotIn("Форма входа не появилась", joined)
        self.assertFalse(any("login_form_missing" in fp for fp, _ in col.files), "не должно быть дампа login_form_missing")

    def test_cloudflare_on_queue_then_cabinet_is_active_session(self):
        """Cloudflare на queue_url сменяется кабинетом — «Сессия активна»,
        дампа «неожиданная разметка» нет."""
        page, context, cfg, account, ws, col = make_world()
        page._html = CABINET_HTML

        async def goto_handler(url):
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.content_hook, _ = make_challenge_then(page, challenge_polls=3)
        page.goto_handler = goto_handler

        with mock.patch("asyncio.sleep", _instant_sleep):
            ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        joined = "\n".join(col.messages)
        self.assertIn("Сессия активна", joined)
        self.assertNotIn("неожиданную разметку", joined)
        self.assertEqual(col.files, [], "успех — дампов не шлём")

    def test_login_form_missing_sends_dump(self):
        """Форма так и не появилась за весь таймаут — False и дамп login_form_missing."""
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            # сессии нет: сайт редиректит на вход, но форма так и не появляется
            page._url = SITE_URL
            page._html = ""

        page.goto_handler = goto_handler
        page.form_timeout = True
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertFalse(ok)
        joined = "\n".join(col.messages)
        self.assertIn("Форма входа не появилась", joined)
        self.assertTrue(col.files)

    def test_successful_login_saves_storage_state(self):
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            if url == QUEUE_URL:
                page._url = SITE_URL  # сессии нет — сайт редиректит на вход
                page._html = LOGIN_HTML
            else:
                page._url = SITE_URL
                page._html = LOGIN_HTML

        async def after_login():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        # HTML-дамп страницы входа тоже должен уйти (диагностика)
        self.assertTrue(any("login_page_dump" in fp for fp, _ in col.files))

    def test_dashboard_landing_is_treated_as_logged_in(self):
        """Сайт часто уводит после входа на дашборд /account/ (user-account-page),
        а не на таблицу выплат. Раньше такой вход ошибочно считался неудачей
        (дамп after_login_unexpected) — сессия не сохранялась и горячий цикл
        не запускался. Теперь дашборд распознаётся как кабинет."""
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = LOGIN_HTML

        async def after_login():
            page._url = "https://x/account/"
            page._html = DASHBOARD_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        self.assertFalse(any("after_login_unexpected" in fp for fp, _ in col.files))

    def test_after_login_captcha_does_not_save_session(self):
        """Ушли со /signin, но открылась капча — это не успех: сессии нет, дамп уходит."""
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            if url == QUEUE_URL:
                page._url = SITE_URL
                page._html = LOGIN_HTML
            else:
                page._url = SITE_URL
                page._html = LOGIN_HTML

        async def after_login():
            page._url = "https://x/check"       # не /signin…
            page._html = CAPTCHA_HTML            # …но и не кабинет

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertFalse(ok)
        self.assertFalse(ws.logged_in)
        self.assertEqual(context.storage_calls, [])
        joined = "\n".join(col.messages)
        self.assertIn("не похожа на кабинет", joined)
        self.assertTrue(col.files)

    def test_cloudflare_challenge_after_login_is_waited_out(self):
        """После успешного ввода логина/пароля сайт отдал Cloudflare-проверку
        «Один момент…» (ровно это видно в реальном дампе
        debug_after_login_unexpected_*). Это НЕ провал: бот пережидает проверку
        и сохраняет сессию, а не помечает вход неудачей — иначе горячий цикл
        так и не запускается."""
        page, context, cfg, account, ws, col = make_world()

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = LOGIN_HTML

        async def after_login():
            # Cloudflare-проверка появляется ТОЛЬКО после отправки формы входа.
            page._url = QUEUE_URL
            page._html = CABINET_HTML
            page.content_hook, _ = make_challenge_then(page, challenge_polls=2)

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        with mock.patch("asyncio.sleep", _instant_sleep):
            ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        self.assertFalse(any("after_login_unexpected" in fp for fp, _ in col.files))

    def test_cloudflare_after_login_then_signin_is_failure(self):
        """Если после Cloudflare-проверки сайт вернул на /signin — вход НЕ удался:
        сессия не сохраняется, уходит дамп after_login."""
        page, context, cfg, account, ws, col = make_world()
        calls = {"n": 0}

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = LOGIN_HTML

        async def content_hook():
            calls["n"] += 1
            if calls["n"] <= 2:
                return CHALLENGE_HTML
            page._url = SITE_URL     # проверка не прошла — снова страница входа
            return LOGIN_HTML

        async def after_login():
            page._url = "https://x/check"   # ещё не /signin, но и не кабинет
            page._html = LOGIN_HTML
            page.content_hook = content_hook

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        with mock.patch("asyncio.sleep", _instant_sleep):
            ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertFalse(ok)
        self.assertFalse(ws.logged_in)
        self.assertEqual(context.storage_calls, [])
        self.assertTrue(any("after_login" in fp for fp, _ in col.files))

    def test_successful_login_with_2fa(self):
        page, context, cfg, account, ws, col = make_world()
        ws.submit_2fa_code("123456")  # заранее кладём код в очередь

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = LOGIN_HTML

        async def after_login():
            page._url = SITE_URL  # остаёмся на /signin, появилась форма кода
            page._html = TWOFA_HTML
            page.show_2fa_after_login = True

        async def after_2fa():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        page.after_2fa_handler = after_2fa
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        self.assertTrue(any("Введите код 2FA" in m for m in col.messages))

    def test_spa_3step_login_without_2fa(self):
        """Новый SPA-вход: Email -> «Далее» -> Password -> «Войти» (без 2FA).

        Шаги сменяют друг друга на одной странице, поля размечены id, кнопки —
        текстом. Бот должен пройти оба шага и сохранить сессию.
        """
        page, context, cfg, account, ws, col = make_world()
        page.spa_login = True

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = SPA_LOGIN_HTML

        async def after_login():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        # Оба шага действительно заполнены, кнопки нажаты.
        self.assertIn((SEL_SPA_EMAIL_INPUT, "a@b.c"), page.fill_log)
        self.assertIn((SEL_SPA_PASSWORD_INPUT, "p"), page.fill_log)
        self.assertIn(SEL_SPA_NEXT_BUTTON, page.click_log)
        self.assertIn(SEL_SPA_SUBMIT_BUTTON, page.click_log)
        # Старую форму не трогали.
        self.assertNotIn(SEL_LOGIN_SUBMIT, page.click_log)

    def test_spa_password_in_dom_but_hidden_still_clicks_next(self):
        """Регрессия: поле пароля ЕСТЬ в разметке, но скрыто (display:none).

        Реальная страница 911pay держит оба шага в DOM одновременно и прячет
        неактивный. Проверка по наличию поля (#password существует) считала шаг
        пароля активным, бот пропускал «Далее» и падал по таймауту на заполнении
        скрытого поля — браузер закрывался. Теперь смотрим ВИДИМОСТЬ.
        """
        page, context, cfg, account, ws, col = make_world()
        page.spa_login = True
        # Ключевое отличие от обычного SPA-теста: поле пароля уже в DOM,
        # но невидимо — как на настоящем сайте.
        page.spa_password_in_dom = True
        page.spa_password_visible = False

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = SPA_LOGIN_HTML

        async def after_login():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        # Главное: «Далее» нажата, несмотря на то что #password был в разметке.
        self.assertIn(SEL_SPA_NEXT_BUTTON, page.click_log)
        self.assertIn((SEL_SPA_EMAIL_INPUT, "a@b.c"), page.fill_log)
        self.assertIn((SEL_SPA_PASSWORD_INPUT, "p"), page.fill_log)
        self.assertIn(SEL_SPA_SUBMIT_BUTTON, page.click_log)

    def test_spa_3step_login_with_2fa(self):
        """Новый SPA-вход + 2FA: код вводится в #twofa-code, затем «Войти».

        После шага пароля SPA остаётся на том же роуте (/signin) и показывает шаг
        кода. Раньше ожидание 2FA опиралось только на URL и пропускало этот шаг —
        теперь поле кода проверяется до URL.
        """
        page, context, cfg, account, ws, col = make_world()
        page.spa_login = True
        ws.submit_2fa_code("123456")  # заранее кладём код в очередь

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = SPA_LOGIN_HTML

        async def after_login():
            page._url = SITE_URL  # остаёмся на /signin, показан шаг кода
            page._html = SPA_TWOFA_HTML
            page.show_2fa_after_login = True

        async def after_2fa():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        page.after_2fa_handler = after_2fa
        ok = asyncio.run(ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file))
        self.assertTrue(ok)
        self.assertTrue(ws.logged_in)
        self.assertEqual(context.storage_calls, [STORAGE_PATH])
        self.assertTrue(any("Введите код 2FA" in m for m in col.messages))
        self.assertIn((SEL_SPA_2FA_INPUT, "123456"), page.fill_log)


class _FakeLocator:
    """Локатор-заглушка: «видимый/невидимый», клики пишет в общий список."""

    def __init__(self, visible, log, name):
        self.first = self
        self._visible = visible
        self._log = log
        self._name = name

    async def count(self):
        return 1 if self._visible else 0

    async def is_visible(self):
        return self._visible

    async def click(self, timeout=None):
        self._log.append(self._name)


class _FakeFrameLocator:
    """frame_locator(i).locator(sel) — «видим» только для заданных селекторов."""

    def __init__(self, visible_selectors, log, prefix):
        self._visible = visible_selectors
        self._log = log
        self._prefix = prefix

    def locator(self, selector):
        return _FakeLocator(
            selector in self._visible, self._log, f"{self._prefix}:{selector}"
        )


class _FakeFrame:
    """Кадр (iframe) для проверки клика внутри Turnstile через page.frames."""

    def __init__(self, url, checkbox_visible, log, prefix="frame"):
        self.url = url
        self._checkbox_visible = checkbox_visible
        self._log = log
        self._prefix = prefix

    def locator(self, selector):
        visible = self._checkbox_visible and selector == "input[type='checkbox']"
        return _FakeLocator(visible, self._log, f"{self._prefix}:{selector}")

    def get_by_role(self, role, **kwargs):
        visible = self._checkbox_visible and role == "checkbox"
        return _FakeLocator(visible, self._log, f"{self._prefix}:role={role}")


class _FakeTurnstilePage:
    """Page-заглушка: iframe-виджет Turnstile и/или managed challenge без iframe."""

    def __init__(self, iframe_checkbox=False, stage_checkbox=False, frames=None):
        self.clicks = []
        self.iframe_visible = {"input[type='checkbox']"} if iframe_checkbox else set()
        self.stage_checkbox = stage_checkbox
        self.frames = frames or []

    def frame_locator(self, selector):
        return _FakeFrameLocator(self.iframe_visible, self.clicks, "iframe")

    def locator(self, selector):
        visible = (
            {"#challenge-stage input[type='checkbox']"} if self.stage_checkbox else set()
        )
        return _FakeLocator(selector in visible, self.clicks, f"page:{selector}")


class TurnstileClickTest(unittest.TestCase):
    """Клик по Cloudflare «Verify you are human» (Turnstile) — без браузера."""

    def test_clicks_checkbox_inside_iframe(self):
        page = _FakeTurnstilePage(iframe_checkbox=True)
        sent = []

        async def notify(text):
            sent.append(text)

        ok = asyncio.run(_click_turnstile(page, notify))
        self.assertTrue(ok)
        self.assertEqual(page.clicks, ["iframe:input[type='checkbox']"])
        self.assertTrue(any("Verify you are human" in m for m in sent))

    def test_clicks_managed_challenge_without_iframe(self):
        page = _FakeTurnstilePage(stage_checkbox=True)
        ok = asyncio.run(_click_turnstile(page))
        self.assertTrue(ok)
        self.assertEqual(page.clicks, ["page:#challenge-stage input[type='checkbox']"])

    def test_clicks_checkbox_in_turnstile_frame_by_url(self):
        """Реальный managed challenge: чекбокс — во вложенном кадре
        challenges.cloudflare.com. Клик находим через page.frames (а не
        одноуровневым frame_locator, куда вложенный кадр не попадает)."""
        log = []
        frame = _FakeFrame("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x", True, log)
        page = _FakeTurnstilePage(frames=[frame])
        ok = asyncio.run(_click_turnstile(page))
        self.assertTrue(ok)
        self.assertEqual(log, ["frame:role=checkbox"])

    def test_ignores_frames_without_cloudflare_url(self):
        log = []
        frame = _FakeFrame("https://spark-trading.cash/account/payouts/", True, log)
        page = _FakeTurnstilePage(frames=[frame])
        ok = asyncio.run(_click_turnstile(page))
        self.assertFalse(ok)
        self.assertEqual(log, [])

    def test_clicks_checkbox_in_blob_turnstile_frame(self):
        """Turnstile-iframe бывает с blob:-URL (без домена Cloudflare) — тоже кликаем."""
        log = []
        frame = _FakeFrame("blob:https://spark-trading.cash/abc", True, log)
        page = _FakeTurnstilePage(frames=[frame])
        self.assertTrue(asyncio.run(_click_turnstile(page)))
        self.assertEqual(log, ["frame:role=checkbox"])

    def test_turnstile_frame_url_detection(self):
        self.assertTrue(_is_turnstile_frame_url("https://challenges.cloudflare.com/x"))
        self.assertTrue(_is_turnstile_frame_url("blob:https://x/abc"))
        self.assertTrue(_is_turnstile_frame_url("about:blank"))
        self.assertFalse(_is_turnstile_frame_url("https://spark-trading.cash/account/"))

    def test_returns_false_when_no_checkbox(self):
        page = _FakeTurnstilePage()
        ok = asyncio.run(_click_turnstile(page))
        self.assertFalse(ok)
        self.assertEqual(page.clicks, [])

    def test_clicks_only_once_even_if_checkbox_stays(self):
        page = _FakeTurnstilePage(iframe_checkbox=True)
        self.assertTrue(asyncio.run(_click_turnstile(page)))
        self.assertTrue(asyncio.run(_click_turnstile(page)))
        # Хелпер делает один клик за вызов — повтор контролирует вызывающий цикл.
        self.assertEqual(len(page.clicks), 2)


class DebugDumpTest(unittest.TestCase):
    """DEBUG=false: дампы сохраняются локально, но в Telegram не шлются (не спамим)."""

    def _dump(self, debug):
        page = FakePage()
        page._url = QUEUE_URL
        page._html = LOGIN_HTML
        col = Collector()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(log_path=str(Path(tmp) / "actions.log"), debug=debug)
            html = asyncio.run(
                _send_debug_dump(page, cfg, col.notify, col.notify_file, "unit", "caption")
            )
            saved = sorted(Path(tmp).glob("debug_unit_*.html"))
        return html, col, saved

    def test_debug_off_saves_locally_but_silent(self):
        html, col, saved = self._dump(debug=False)
        self.assertEqual(html, LOGIN_HTML)
        self.assertEqual(col.messages, [])  # в Telegram ничего не ушло
        self.assertEqual(col.files, [])
        self.assertTrue(saved, "HTML-дамп должен сохраниться на диск для разбора")

    def test_debug_on_sends_to_telegram(self):
        html, col, saved = self._dump(debug=True)
        self.assertTrue(col.messages)
        self.assertEqual(len(col.files), 2)  # HTML + PNG
        self.assertTrue(saved)


class _ActionPage:
    """Мини-страница с evaluate/press: проверяет фолбэки «Далее» и снятие фокуса.

    FakePage этих методов не имеет (getattr → None), поэтому ветки, ради которых
    делались фиксы входа, в прежних тестах молча пропускались. Здесь пишем все
    вызовы, чтобы проверить порядок действий и что ни один фолбэк не роняет вход.
    """

    def __init__(self, click_fails=False, press_fails=False, eval_fails=False,
                 click_ok_after_unlock=False):
        self.events = []
        self.click_fails = click_fails
        self.press_fails = press_fails
        self.eval_fails = eval_fails
        # «Далее» disabled, пока evaluate не снимет атрибут disabled в DOM.
        self.click_ok_after_unlock = click_ok_after_unlock
        self.unlocked = False

    async def evaluate(self, script, *args):
        self.events.append(("evaluate", script, args))
        if self.eval_fails:
            raise RuntimeError("evaluate failed")
        if "removeAttribute" in script:
            self.unlocked = True

    async def press(self, selector, key):
        self.events.append(("press", selector, key))
        if self.press_fails:
            raise RuntimeError("press failed")

    async def click(self, selector, timeout=None):
        self.events.append(("click", selector, timeout))
        if self.click_fails and not (self.click_ok_after_unlock and self.unlocked):
            raise TimeoutError("element is not enabled")


class _DisabledUntilBlurPage(FakePage):
    """«Далее» disabled, пока поле email не потеряло фокус — поведение Vue на 911pay.

    Без `_blur_active` клик падал бы по таймауту: ровно тот симптом, с которым
    пришёл пользователь («поле заполнилось, кнопка не нажалась, браузер закрылся»).
    """

    def __init__(self):
        super().__init__()
        self.blurred = False
        self.eval_log = []

    async def evaluate(self, script, *args):
        self.eval_log.append(script)
        if "blur" in script:
            self.blurred = True

    async def press(self, selector, key):
        # Enter в поле email отправляет ту же форму, что и кнопка type="submit".
        if selector == SEL_SPA_EMAIL_INPUT and key == "Enter":
            self.blurred = True

    async def click(self, selector, timeout=None):
        if selector == SEL_SPA_NEXT_BUTTON and not self.blurred:
            # Playwright ждёт actionability (снятия disabled) и падает по таймауту.
            raise TimeoutError("element is not enabled")
        await super().click(selector, timeout)


class SpaNextStepTest(unittest.TestCase):
    """Кнопка «Далее» включается по blur, а не по input — проверяем фолбэки.

    Симптом с прода: `page.fill()` заполняет email, но не шлёт blur, Vue не снимает
    disabled, Playwright ждёт actionability до таймаута — клика нет, браузер
    закрывается. Фикс: снять фокус, кликнуть с коротким таймаутом, затем Enter,
    затем снять disabled в DOM.
    """

    def test_blur_active_blurs_focused_element(self):
        page = _ActionPage()
        asyncio.run(_blur_active(page))
        self.assertEqual([e[0] for e in page.events], ["evaluate"])
        self.assertIn("blur", page.events[0][1])

    def test_blur_active_is_noop_without_evaluate_or_press(self):
        # У FakePage нет evaluate/press — вход не должен падать.
        asyncio.run(_blur_active(FakePage()))

    def test_blur_active_falls_back_to_tab_when_evaluate_fails(self):
        page = _ActionPage(eval_fails=True)
        asyncio.run(_blur_active(page))
        self.assertEqual([e[0] for e in page.events], ["evaluate", "press"])
        self.assertEqual(page.events[1][2], "Tab")

    def test_click_next_blurs_first_then_clicks(self):
        page = _ActionPage()
        asyncio.run(_click_next_step(page, SEL_SPA_EMAIL_INPUT, SEL_SPA_NEXT_BUTTON))
        self.assertEqual([e[0] for e in page.events], ["evaluate", "click"])
        self.assertIn("blur", page.events[0][1])
        self.assertEqual(page.events[1][1], SEL_SPA_NEXT_BUTTON)
        # Короткий таймаут: не ждём 15 с на заведомо выключенной кнопке.
        self.assertEqual(page.events[1][2], 5000)

    def test_click_next_falls_back_to_enter(self):
        page = _ActionPage(click_fails=True)
        asyncio.run(_click_next_step(page, SEL_SPA_EMAIL_INPUT, SEL_SPA_NEXT_BUTTON))
        self.assertEqual([e[0] for e in page.events], ["evaluate", "click", "press"])
        self.assertEqual(page.events[2][1], SEL_SPA_EMAIL_INPUT)
        self.assertEqual(page.events[2][2], "Enter")

    def test_click_next_removes_disabled_when_enter_also_fails(self):
        page = _ActionPage(
            click_fails=True, press_fails=True, click_ok_after_unlock=True
        )
        asyncio.run(_click_next_step(page, SEL_SPA_EMAIL_INPUT, SEL_SPA_NEXT_BUTTON))
        self.assertEqual(
            [e[0] for e in page.events],
            ["evaluate", "click", "press", "evaluate", "click"],
        )
        # Последний клик прошёл только после снятия disabled в DOM.
        self.assertTrue(page.unlocked)

    def test_click_next_never_raises_when_every_fallback_fails(self):
        page = _ActionPage(click_fails=True, press_fails=True, eval_fails=True)
        # Ни клик, ни Enter, ни снятие disabled не удались — вход не падает.
        asyncio.run(_click_next_step(page, SEL_SPA_EMAIL_INPUT, SEL_SPA_NEXT_BUTTON))
        self.assertEqual(
            [e[0] for e in page.events],
            ["evaluate", "press", "click", "press", "evaluate"],
        )

    def test_login_succeeds_when_next_button_disabled_until_blur(self):
        """Регрессия к симптому с прода: без снятия фокуса «Далее» не нажимается."""
        page = _DisabledUntilBlurPage()
        _, context, cfg, account, ws, col = make_world()
        page.spa_login = True

        async def goto_handler(url):
            page._url = SITE_URL
            page._html = SPA_LOGIN_HTML

        async def after_login():
            page._url = QUEUE_URL
            page._html = CABINET_HTML

        page.goto_handler = goto_handler
        page.after_login_handler = after_login
        ok = asyncio.run(
            ensure_logged_in(page, context, cfg, account, ws, col.notify, col.notify_file)
        )
        self.assertTrue(ok, "вход должен пройти: _blur_active снимает disabled у «Далее»")
        self.assertTrue(ws.logged_in)
        # Фокус действительно снимали: без этого «Далее» осталась бы disabled.
        self.assertTrue(any("blur" in s for s in page.eval_log))
        self.assertTrue(page.blurred)
        self.assertIn(SEL_SPA_NEXT_BUTTON, page.click_log)
        self.assertIn(SEL_SPA_SUBMIT_BUTTON, page.click_log)


if __name__ == "__main__":
    unittest.main()