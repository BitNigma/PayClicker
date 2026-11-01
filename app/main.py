import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram.error import Conflict

# Прямой запуск файла (`python app/main.py`) выставляет __package__ = "" и тогда
# относительные импорты ниже падают с ImportError: attempted relative import
# with no known parent package. Добавляем корень проекта в sys.path и объявляем
# пакет, чтобы работали ОБА способа запуска: `python -m app.main` и
# `python app/main.py`. Блок обязан идти ДО относительных импортов.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "app"  # noqa: A001 — так относительные импорты резолвятся в app.*

from .config import load_config  # noqa: E402
from .state import state  # noqa: E402
from .telegram_bot import build_bot_app  # noqa: E402

load_dotenv()  # локально читает .env; в Docker переменные уже в окружении

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def _pick_worker():
    """WORKER_BACKEND=http  — горячий цикл на httpx, без браузера (по умолчанию).
    WORKER_BACKEND=browser — прежний Playwright-воркер (безопасный откат)."""
    backend = os.environ.get("WORKER_BACKEND", "http").lower()
    if backend == "browser":
        from .browser_worker import run_worker
    else:
        from .http_worker import run_worker
    logger.info("Worker backend: %s", backend)
    return run_worker, backend


async def _warn_if_duplicate_instance(application) -> None:
    """Проверяем, не опрашивает ли бота ДРУГОЙ процесс (409 Conflict).

    Частый случай: рядом с `docker compose up` запускают ещё и локальный
    `python -m app.main`. Telegram разрешает только один getUpdates, поэтому
    второй экземпляр получает `telegram.error.Conflict`, а telegram.ext пишет в
    лог многострочный traceback «Exception happened while polling» — со стороны
    это выглядит как падение сервиса. Здесь объясняем причину заранее.

    Проба безопасна: offset=-1 отдаёт последний апдейт, НЕ подтверждая его
    (подтверждение происходит только при offset = update_id + 1), поэтому
    start_polling() следом получит все накопившиеся апдейты как обычно.
    """
    try:
        await application.bot.get_updates(offset=-1, timeout=0, limit=1)
    except Conflict:
        logger.error(
            "Обнаружен ДРУГОЙ запущенный экземпляр этого бота (409 Conflict): "
            "getUpdates уже кем-то опрашивается. Остановите лишний процесс "
            "(например, локальный запуск при работающем docker compose) — иначе "
            "апдейты будут теряться и в логе будут ошибки polling."
        )
    except Exception as exc:  # сеть недоступна и т.п. — не мешаем запуску
        logger.warning("Не удалось проверить дубликат экземпляра бота: %s", exc)


def _on_polling_error(exc: Exception) -> None:
    """Колбэк ошибок long-polling (передаётся в start_polling).

    Штатный поведение telegram.ext — залогировать КАЖДУЮ ошибку опроса полным
    многострочным traceback'ом. Для 409 Conflict это создаёт ложное впечатление
    крэша, хотя процесс жив и продолжает работать. Здесь Conflict объясняется
    одной понятной строкой, остальные ошибки логируются как раньше.
    """
    if isinstance(exc, Conflict):
        logger.error(
            "409 Conflict: бота опрашивает ДРУГОЙ экземпляр (getUpdates уже занят). "
            "Это НЕ крэш — процесс продолжает работу, но апдейты может забирать "
            "другой процесс. Остановите лишний экземпляр (например, локальный "
            "запуск при работающем docker compose) и перезапустите этот."
        )
        return
    logger.error("Ошибка при опросе Telegram: %s: %s", type(exc).__name__, exc)


def _shadowed_env(env_file: Path) -> dict:
    """Ключи, заданные И в .env, И в окружении — с РАЗНЫМИ значениями.

    `load_dotenv()` по умолчанию не перезаписывает уже существующие переменные,
    поэтому старый `export TARGET_QUEUE_SIZE=10` в терминале тихо подменяет
    значение из .env. Возвращает {ключ: (значение_окружения, значение_файла)}.
    """
    try:
        from dotenv import dotenv_values

        from_file = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    except Exception:
        return {}
    return {
        key: (os.environ[key], value)
        for key, value in from_file.items()
        if key in os.environ and os.environ[key] != value
    }


def _warn_shadowed_env(env_file: Path | None = None) -> None:
    """Громко предупреждает, что терминал перекрывает .env.

    Иначе это выглядит как «в .env написано 8, а бот пишет цель 10». Секреты
    (токены/пароли/логины) не печатаем — только факт расхождения.
    """
    env_file = env_file or (Path(__file__).resolve().parents[1] / ".env")
    shadowed = _shadowed_env(env_file)
    if not shadowed:
        return
    logger.warning(
        "Переменные окружения перекрывают .env (%d шт.): load_dotenv() их не "
        "перезаписывает, бот возьмёт значения из терминала. Выполните `unset …` "
        "или запустите в чистом терминале:",
        len(shadowed),
    )
    for key, (shell_value, file_value) in sorted(shadowed.items()):
        if any(word in key for word in ("TOKEN", "PASSWORD", "SECRET", "LOGIN")):
            logger.warning("  %s: значения разные (скрыты)", key)
        else:
            logger.warning("  %s: в окружении %r, в .env %r", key, shell_value, file_value)


def _run_self_check() -> int:
    """`python app/main.py --check` — проверить импорты и конфиг БЕЗ запуска бота.

    Полезно, когда сервис «не стартует»: команда показывает, читается ли .env,
    сколько кабинетов и админов распознано, и не требует сети/Telegram.
    """
    _warn_shadowed_env()
    cfg = load_config()
    run_worker, backend = _pick_worker()
    logger.info(
        "Проверка OK: backend=%s, кабинетов=%d (%s), админов=%d, прокси=%d, "
        "цель=%d заявок, headless=%s",
        backend,
        len(cfg.accounts),
        ", ".join(f"{a.index}:{a.name}" for a in cfg.accounts),
        len(cfg.allowed_chat_ids),
        len(getattr(cfg, "proxy_urls", []) or []),
        cfg.target_queue_size,
        cfg.headless,
    )
    logger.info("Точка входа: %s", getattr(run_worker, "__module__", run_worker))
    return 0


async def main():
    _warn_shadowed_env()
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

    # Браузерный воркер (WORKER_BACKEND=browser, «безопасный откат») поднимает
    # отдельный Chromium на кабинет и использует общие data-пути, поэтому в
    # мультиаккаунте запускаем им только первый кабинет. Для нескольких
    # кабинетов сразу — WORKER_BACKEND=http (по умолчанию).
    accounts = cfg.accounts
    if backend == "browser" and len(cfg.accounts) > 1:
        logger.warning(
            "WORKER_BACKEND=browser поддерживает только кабинет #1: запускаю лишь «%s». "
            "Для одновременной работы нескольких кабинетов используйте WORKER_BACKEND=http",
            cfg.accounts[0].name,
        )
        accounts = cfg.accounts[:1]

    async with application:
        await application.start()
        # Проверяем дубликат ДО start_polling: иначе второй экземпляр зальёт лог
        # многострочными traceback'ами Conflict и это будет похоже на крэш.
        await _warn_if_duplicate_instance(application)
        # error_callback превращает 409 Conflict из «страшного traceback» в одну
        # понятную строку: конфликт двух экземпляров — не крэш процесса.
        await application.updater.start_polling(error_callback=_on_polling_error)
        logger.info("Telegram bot polling started; admins=%s", cfg.allowed_chat_ids)

        # Свой воркер на каждый кабинет: задача живёт постоянно и ждёт,
        # пока бот не выставит ws.running=True (команда /run + выбор кабинета).
        worker_tasks = []
        for account in accounts:
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
            [f"{a.index}:{a.name}" for a in accounts],
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
    # --check — быстрая проверка окружения/конфига без запуска бота и без сети.
    if any(arg in ("--check", "-c") for arg in sys.argv[1:]):
        sys.exit(_run_self_check())
    asyncio.run(main())

