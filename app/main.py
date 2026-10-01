import asyncio
import logging

from dotenv import load_dotenv

from .config import load_config
from .state import state
from .telegram_bot import build_bot_app

load_dotenv()  # локально читает .env; в Docker переменные уже в окружении

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def _pick_worker():
    """WORKER_BACKEND=http  — горячий цикл на httpx, без браузера (по умолчанию).
    WORKER_BACKEND=browser — прежний Playwright-воркер (безопасный откат)."""
    import os

    backend = os.environ.get("WORKER_BACKEND", "http").lower()
    if backend == "browser":
        from .browser_worker import run_worker
    else:
        from .http_worker import run_worker
    logger.info("Worker backend: %s", backend)
    return run_worker, backend


async def main():
    cfg = load_config()
    run_worker, backend = _pick_worker()
    application = build_bot_app(cfg)

    # notify() — как воркер шлёт сообщения пользователю в телеграм (всем админам)
    async def notify(text: str):
        for chat_id in cfg.allowed_chat_ids:
            try:
                await application.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("Failed to send telegram notification to %s", chat_id)

    async def notify_file(path: str, caption: str = ""):
        for chat_id in cfg.allowed_chat_ids:
            try:
                with open(path, "rb") as f:
                    await application.bot.send_document(
                        chat_id=chat_id, document=f, caption=caption
                    )
            except Exception:
                logger.exception("Failed to send telegram document to %s", chat_id)

    if backend == "browser" and len(cfg.accounts) > 1:
        logger.warning(
            "WORKER_BACKEND=browser поддерживает только кабинет #1; для %d кабинетов "
            "используйте WORKER_BACKEND=http",
            len(cfg.accounts),
        )

    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram bot polling started; admins=%s", cfg.allowed_chat_ids)

        # Свой воркер на каждый кабинет: задача живёт постоянно и ждёт,
        # пока бот не выставит ws.running=True (команда /run + выбор кабинета).
        worker_tasks = []
        for account in cfg.accounts:
            ws = state.ensure(account.index, account.name)
            worker_tasks.append(
                asyncio.create_task(
                    run_worker(cfg, account, ws, notify, notify_file),
                    name=f"worker-{account.index}",
                )
            )
        logger.info(
            "Started %d worker task(s): %s",
            len(worker_tasks),
            [f"{a.index}:{a.name}" for a in cfg.accounts],
        )

        try:
            await asyncio.gather(*worker_tasks)
        finally:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())

