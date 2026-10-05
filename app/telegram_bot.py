from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .config import Config
from .state import state

logger = logging.getLogger("bot")


def _check_chat(cfg: Config, update: Update) -> bool:
    """Пускаем только id из списка админов. Остальных тихо логируем и игнорируем."""
    chat = update.effective_chat
    if chat is not None and chat.id in cfg.allowed_chat_ids:
        return True
    logger.info("Отклонено сообщение от не-админа chat_id=%s", chat.id if chat else None)
    return False


# ------------------------------------------------------------------
# Мультиаккаунт: выбор кабинета для /run, /stop, /watch и маршрутизация 2FA
# ------------------------------------------------------------------
def _account_status(ws) -> str:
    """Короткое состояние кабинета для списков в ответах бота."""
    if ws is None:
        return "не запускался"
    if ws.awaiting_2fa:
        return "⏳ жду код 2FA"
    if ws.running:
        return f"▶️ работает ({ws.mode})"
    return "⏹ остановлен"


def _accounts_list(cfg: Config) -> str:
    """Список кабинетов: «номер. имя — состояние (, взято N)»."""
    lines = []
    for account in cfg.accounts:
        ws = state.workers.get(account.index)
        extra = f", взято заявок: {ws.taken_count}" if ws and ws.taken_count else ""
        lines.append(f"{account.index}. {account.name} — {_account_status(ws)}{extra}")
    return "\n".join(lines)


async def _ask_account(update: Update, cfg: Config, action: str, verb: str) -> None:
    """Кладёт диалог «какой кабинет?» в chat_pending и просит прислать номера."""
    state.chat_pending[update.effective_chat.id] = {"action": action, "step": "choose_account"}
    await update.message.reply_text(
        f"Какой кабинет {verb}?\n{_accounts_list(cfg)}\n\n"
        "Пришлите номер цифрами (например: 1), несколько номеров через пробел (1 3) "
        "или all — тогда действие применится ко всем кабинетам сразу."
    )


# Слова, означающие «все кабинеты» (в диалоге и в аргументах команд).
_ALL_WORDS = {"all", "все", "всё", "*"}

# Заголовки сводки для действий сразу по нескольким кабинетам.
_ACTION_TITLES = {
    "run": "▶️ Запускаю на взятие заявок",
    "watch": "👀 Включаю наблюдение",
    "stop": "⏹ Останавливаю",
    "start": "🚀 Запускаю",
    "reset": "🧹 Сбрасываю кэш очереди /reset_session",
}


def _parse_account_indices(text: str, cfg: Config) -> list:
    """Разбирает выбор кабинетов: '2' -> [2], '1 3' / '1,3' -> [1, 3],
    'all'/'все' -> номера всех кабинетов. Непонятный ввод -> [] (пустой список)."""
    cleaned = (text or "").strip().lower()
    if cleaned in _ALL_WORDS:
        return [a.index for a in cfg.accounts]
    known = {a.index for a in cfg.accounts}
    chosen = []
    for token in re.split(r"[\s,;/]+", cleaned):
        if not token:
            continue
        if not re.fullmatch(r"\d{1,3}", token):
            return []
        index = int(token)
        if index not in known or index in chosen:
            return []  # неизвестный/повторяющийся номер — не угадываем
        chosen.append(index)
    return chosen


def _parse_account_index(text: str, cfg: Config) -> int | None:
    """'2' -> 2, если кабинет с таким номером есть; иначе None."""
    indices = _parse_account_indices(text, cfg)
    return indices[0] if len(indices) == 1 else None


def _cmd_args(context) -> list:
    """Аргументы команды (/run 1 2 -> ['1', '2']). context в тестах может быть None."""
    args = getattr(context, "args", None) or []
    return [str(a) for a in args if str(a).strip()]


async def _apply_account_action(update: Update, cfg: Config, action: str, index) -> None:
    """Запускает/останавливает один кабинет (номер) или сразу несколько (список).

    Отвечает одной сводкой: по строке на кабинет, чтобы при массовом запуске
    было видно, что именно произошло с каждым.
    """
    indices = [index] if isinstance(index, int) else list(index)
    lines = []
    need_login = []
    for i in indices:
        account = next((a for a in cfg.accounts if a.index == i), None)
        if account is None:
            continue
        ws = state.ensure(account.index, account.name)

        if action == "stop":
            ws.running = False
            lines.append(f"⏹ Кабинет «{ws.name}» остановлен.")
            continue

        if action == "watch":
            ws.mode = "watch"
        elif action == "run":
            ws.mode = "take"
        # action == "start" — режим не меняем, только запускаем
        # action == "reset" — не меняем ни режим, ни running, только кэш очереди

        if action == "reset":
            had_stale = bool(ws.taken_order_ids or ws.demands_ids or ws.taken_count)
            ws.reset_queue_cache()
            if had_stale:
                lines.append(
                    f"🧹 Кабинет «{ws.name}»: кэш очереди очищен, работаю по данным площадки."
                )
            else:
                lines.append(f"ℹ️ Кабинет «{ws.name}»: кэш очереди был пуст. Ничего не очищал.")
            continue

        already = ws.running
        ws.running = True
        if already:
            lines.append(f"ℹ️ Кабинет «{ws.name}» уже работал — продолжаю (режим {ws.mode}).")
        elif ws.logged_in:
            lines.append(f"▶️ Кабинет «{ws.name}»: режим {ws.mode} запущен.")
        else:
            need_login.append(ws.name)
            lines.append(f"🚀 Кабинет «{ws.name}»: запускаю авторизацию, мониторинг стартует сразу после.")

    if len(indices) > 1:
        lines.insert(0, f"{_ACTION_TITLES.get(action, action)} — кабинетов: {len(indices)}")

    if need_login:
        lines.append(
            "Если понадобится код 2FA — пришлите его сюда. Когда кода ждут несколько "
            "кабинетов, укажите номер и код через пробел (например: 2 123456)."
        )
    await update.message.reply_text("\n".join(lines))


async def _apply_command_args(update: Update, cfg: Config, action: str, context) -> bool:
    """Применяет действие к кабинетам, перечисленным прямо в команде
    (/run 1 3, /stop all). Возвращает True, если аргументы были и мы ответили,
    и False — если аргументов нет и нужен диалог выбора кабинета."""
    args = _cmd_args(context)
    if not args:
        return False
    indices = _parse_account_indices(" ".join(args), cfg)
    if not indices:
        await update.message.reply_text(
            f"Не понял номера кабинетов: {' '.join(args)}\n{_accounts_list(cfg)}\n\n"
            "Укажите номера через пробел (например: 1 3) или all — все кабинеты."
        )
        return True
    await _apply_account_action(update, cfg, action, indices)
    return True


async def _all_accounts_action(update: Update, cfg: Config, action: str) -> None:
    """Действие сразу по всем кабинетам — для команд /runall, /stopall, /watchall."""
    await _apply_account_action(update, cfg, action, [a.index for a in cfg.accounts])


async def cmd_start_session(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if await _apply_command_args(update, cfg, "start", context):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "start", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "start", "запустить (/start_session)")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if await _apply_command_args(update, cfg, "watch", context):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "watch", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "watch", "включить наблюдение (/watch)")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if await _apply_command_args(update, cfg, "run", context):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "run", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "run", "запустить на взятие заявок (/run)")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if await _apply_command_args(update, cfg, "stop", context):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "stop", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "stop", "остановить (/stop)")


async def cmd_reset_session(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    """Очистка локального кэша очереди кабинета: бот заново считает активную очередь
    по странице demands (а не по накопленной в памяти). Cookies/сессию сайта не трогаем."""
    if not _check_chat(cfg, update):
        return
    if await _apply_command_args(update, cfg, "reset", context):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "reset", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "reset", "сбросить кэш очереди (/reset_session)")


async def cmd_run_all(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    """Запускает ВСЕ кабинеты на взятие заявок (короткая форма /run all)."""
    if not _check_chat(cfg, update):
        return
    await _all_accounts_action(update, cfg, "run")


async def cmd_watch_all(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    """Включает наблюдение во всех кабинетах."""
    if not _check_chat(cfg, update):
        return
    await _all_accounts_action(update, cfg, "watch")


async def cmd_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    """Останавливает ВСЕ кабинеты (короткая форма /stop all)."""
    if not _check_chat(cfg, update):
        return
    await _all_accounts_action(update, cfg, "stop")


async def cmd_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    running = [ws for ws in state.workers.values() if ws.running]
    if not running:
        await update.message.reply_text("Ни один кабинет сейчас не запущен — снимок сделать не с чего.")
        return
    for ws in running:
        ws.snapshot_requested = True
    names = ", ".join(ws.name for ws in running)
    await update.message.reply_text(f"📸 Запросил снимок страницы ({names}), пришлю в течение пары секунд.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    lines = [f"Кабинетов: {len(cfg.accounts)}"]
    running = [
        a.index for a in cfg.accounts
        if (ws := state.workers.get(a.index)) is not None and ws.running
    ]
    lines.append(f"работает сейчас: {len(running)} из {len(cfg.accounts)}" + (f" (№{sorted(running)})" if running else ""))
    for account in cfg.accounts:
        ws = state.workers.get(account.index)
        lines.append(f"\n{account.index}. {account.name} — {_account_status(ws)}")
        if ws is None:
            continue
        lines.append(f"   mode={ws.mode} logged_in={ws.logged_in} взято={ws.taken_count}")
        # Занятые слоты: заявки «в работе» на площадке (в т.ч. автовыдача и взятые
        # до перезапуска процесса) + взятые в этой сессии. Цель — TARGET_QUEUE_SIZE.
        in_work = ws.demands_ids | ws.taken_order_ids
        lines.append(
            f"   в работе={ws.slots_taken()}/{cfg.target_queue_size}"
            + (f" {sorted(in_work)}" if in_work else "")
        )
        lines.append(f"   status={ws.last_status}")
    lines.append(f"\nадминов={len(cfg.allowed_chat_ids)}")
    await update.message.reply_text("\n".join(lines))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    """Показывает chat_id и статус доступа. Работает для ЛЮБОГО, кто написал боту,
    чтобы новый человек мог узнать свой id и передать его владельцу для добавления."""
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    is_admin = chat_id in cfg.allowed_chat_ids
    await update.message.reply_text(
        f"Ваш chat_id: {chat_id}\n"
        f"Доступ: {'есть ✅' if is_admin else 'нет ❌ — передайте этот id владельцу'}"
    )


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    pending = state.chat_pending.get(chat_id)
    awaiting = [ws for ws in state.workers.values() if ws.awaiting_2fa]

    # 1) Активный диалог «какой кабинет?» (после /run, /stop, /watch, /start_session)
    if pending and pending.get("step") == "choose_account":
        indices = _parse_account_indices(text, cfg)
        if not indices:
            # 3-8 цифр при активном ожидании 2FA — скорее всего это код, а не номер кабинета
            if awaiting and re.fullmatch(r"\d{3,8}", text):
                state.chat_pending.pop(chat_id, None)
                pending = None  # проваливаемся в обработку 2FA ниже
            else:
                await update.message.reply_text(
                    "Не понял номер кабинета. Пришлите цифру из списка, "
                    f"несколько номеров через пробел (1 3) или all:\n{_accounts_list(cfg)}"
                )
                return
        else:
            action = pending.get("action", "run")
            state.chat_pending.pop(chat_id, None)
            await _apply_account_action(update, cfg, action, indices)
            return

    # 2) Диалог «номер кабинета + код» — когда код 2FA ждут сразу несколько кабинетов
    if pending and pending.get("step") == "choose_2fa_code":
        m = re.fullmatch(r"(\d{1,3})\D+(\d{3,8})", text)
        if not m:
            await update.message.reply_text(
                "Пришлите номер кабинета и код через пробел, например: 2 123456"
            )
            return
        ws = state.workers.get(int(m.group(1)))
        state.chat_pending.pop(chat_id, None)
        if ws is None or not ws.awaiting_2fa:
            await update.message.reply_text("Этот кабинет больше не ждёт код 2FA.")
            return
        ws.submit_2fa_code(m.group(2))
        await update.message.reply_text(f"Код принят (кабинет «{ws.name}»), проверяю...")
        return

    # 3) Воркер ждёт 2FA-код — принимаем ТОЛЬКО похожие на код сообщения (цифры),
    # чтобы случайные сообщения от других админов не съедались как код.
    if awaiting:
        if not re.fullmatch(r"\d{3,8}", text):
            await update.message.reply_text(
                "Жду код 2FA — пришлите, пожалуйста, только цифры (например, 123456)."
            )
            return
        if len(awaiting) == 1:
            ws = awaiting[0]
            ws.submit_2fa_code(text)
            await update.message.reply_text(f"Код принят (кабинет «{ws.name}»), проверяю...")
        else:
            # Куда отправить код — неизвестно: просим указать номер кабинета
            state.chat_pending[chat_id] = {"action": "2fa", "step": "choose_2fa_code"}
            names = ", ".join(f"{w.index} ({w.name})" for w in awaiting)
            await update.message.reply_text(
                f"Код 2FA ждут сразу несколько кабинетов: {names}.\n"
                "Пришлите номер кабинета и код через пробел, например: 2 123456"
            )
        return

    await update.message.reply_text(
        "Команды: /run /stop /watch /status /snapshot /reset_session /id\n"
        "Несколько кабинетов сразу: /run 1 3 · /run all · /runall "
        "(так же /stop, /watch → /stopall, /watchall).\n"
        "/reset_session — очистить кэш очереди кабинета (пересчёт по площадке), "
        "если счётчик «в работе» разошёлся с реальностью.\n"
        "Без аргументов бот спросит, какие кабинеты запустить — "
        "можно прислать «1 3» или «all»."
    )


def build_bot_app(cfg: Config) -> Application:
    application = Application.builder().token(cfg.telegram_token).build()

    application.add_handler(CommandHandler("start_session", lambda u, c: cmd_start_session(u, c, cfg)))
    application.add_handler(CommandHandler("watch", lambda u, c: cmd_watch(u, c, cfg)))
    application.add_handler(CommandHandler("run", lambda u, c: cmd_run(u, c, cfg)))
    application.add_handler(CommandHandler("stop", lambda u, c: cmd_stop(u, c, cfg)))
    application.add_handler(CommandHandler("reset_session", lambda u, c: cmd_reset_session(u, c, cfg)))
    # Короткие формы для мультиаккаунта — сразу все кабинеты.
    application.add_handler(CommandHandler("runall", lambda u, c: cmd_run_all(u, c, cfg)))
    application.add_handler(CommandHandler("watchall", lambda u, c: cmd_watch_all(u, c, cfg)))
    application.add_handler(CommandHandler("stopall", lambda u, c: cmd_stop_all(u, c, cfg)))
    application.add_handler(CommandHandler("snapshot", lambda u, c: cmd_snapshot(u, c, cfg)))
    application.add_handler(CommandHandler("status", lambda u, c: cmd_status(u, c, cfg)))
    # /id доступен ЛЮБОМУ — чтобы новый человек узнал свой chat_id для добавления
    application.add_handler(CommandHandler("id", lambda u, c: cmd_id(u, c, cfg)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: on_text_message(u, c, cfg)))

    return application
