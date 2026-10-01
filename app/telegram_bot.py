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
    """Кладёт диалог «какой кабинет?» в chat_pending и просит прислать номер."""
    state.chat_pending[update.effective_chat.id] = {"action": action, "step": "choose_account"}
    await update.message.reply_text(
        f"Какой кабинет {verb}?\n{_accounts_list(cfg)}\n\nПришлите номер цифрами (например: 1)."
    )


def _parse_account_index(text: str, cfg: Config) -> int | None:
    """'2' -> 2, если кабинет с таким номером есть; иначе None."""
    if not re.fullmatch(r"\d{1,3}", text):
        return None
    idx = int(text)
    return idx if any(a.index == idx for a in cfg.accounts) else None


async def _apply_account_action(update: Update, cfg: Config, action: str, index: int) -> None:
    """Запускает/останавливает выбранный кабинет."""
    account = next(a for a in cfg.accounts if a.index == index)
    ws = state.ensure(account.index, account.name)

    if action == "stop":
        ws.running = False
        await update.message.reply_text(f"⏹ Кабинет «{ws.name}» остановлен.")
        return

    if action == "watch":
        ws.mode = "watch"
    elif action == "run":
        ws.mode = "take"
    # action == "start" — режим не меняем, только запускаем

    already = ws.running
    ws.running = True
    if already:
        await update.message.reply_text(f"ℹ️ Кабинет «{ws.name}» уже работал — продолжаю (режим {ws.mode}).")
    elif ws.logged_in:
        await update.message.reply_text(f"▶️ Кабинет «{ws.name}»: режим {ws.mode} запущен.")
    else:
        await update.message.reply_text(
            f"🚀 Кабинет «{ws.name}»: запускаю авторизацию, мониторинг стартует сразу после. "
            "Если понадобится код 2FA — пришлите его сюда."
        )


async def cmd_start_session(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "start", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "start", "запустить (/start_session)")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "watch", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "watch", "включить наблюдение (/watch)")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "run", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "run", "запустить на взятие заявок (/run)")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE, cfg: Config):
    if not _check_chat(cfg, update):
        return
    if len(cfg.accounts) == 1:
        await _apply_account_action(update, cfg, "stop", cfg.accounts[0].index)
        return
    await _ask_account(update, cfg, "stop", "остановить (/stop)")


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
    for account in cfg.accounts:
        ws = state.workers.get(account.index)
        lines.append(f"\n{account.index}. {account.name} — {_account_status(ws)}")
        if ws is None:
            continue
        lines.append(f"   mode={ws.mode} logged_in={ws.logged_in} взято={ws.taken_count}")
        if ws.taken_order_ids:
            lines.append(f"   очередь={len(ws.taken_order_ids)} {sorted(ws.taken_order_ids)}")
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
        index = _parse_account_index(text, cfg)
        if index is None:
            # 3-8 цифр при активном ожидании 2FA — скорее всего это код, а не номер кабинета
            if awaiting and re.fullmatch(r"\d{3,8}", text):
                state.chat_pending.pop(chat_id, None)
                pending = None  # проваливаемся в обработку 2FA ниже
            else:
                await update.message.reply_text(
                    f"Не понял номер кабинета. Пришлите цифру из списка:\n{_accounts_list(cfg)}"
                )
                return
        else:
            action = pending.get("action", "run")
            state.chat_pending.pop(chat_id, None)
            await _apply_account_action(update, cfg, action, index)
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
        "Команды: /run /stop /watch /status /snapshot /id\n"
        "Пример: /run — бот спросит, какой кабинет запустить, пришлите номер цифрами."
    )


def build_bot_app(cfg: Config) -> Application:
    application = Application.builder().token(cfg.telegram_token).build()

    application.add_handler(CommandHandler("start_session", lambda u, c: cmd_start_session(u, c, cfg)))
    application.add_handler(CommandHandler("watch", lambda u, c: cmd_watch(u, c, cfg)))
    application.add_handler(CommandHandler("run", lambda u, c: cmd_run(u, c, cfg)))
    application.add_handler(CommandHandler("stop", lambda u, c: cmd_stop(u, c, cfg)))
    application.add_handler(CommandHandler("snapshot", lambda u, c: cmd_snapshot(u, c, cfg)))
    application.add_handler(CommandHandler("status", lambda u, c: cmd_status(u, c, cfg)))
    # /id доступен ЛЮБОМУ — чтобы новый человек узнал свой chat_id для добавления
    application.add_handler(CommandHandler("id", lambda u, c: cmd_id(u, c, cfg)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: on_text_message(u, c, cfg)))

    return application
