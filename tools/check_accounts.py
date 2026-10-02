"""Проверка конфигурации кабинетов перед запуском (в т.ч. мультиаккаунта).

Показывает, какие кабинеты увидит бот, где лежат файлы их сессий и совместим ли
выбранный WORKER_BACKEND с несколькими кабинетами. Секреты не печатает: логин
маскируется, пароль не выводится вовсе.

Запуск из корня проекта (интерпретатором проекта, Python 3.10+):

    python tools/check_accounts.py                # читает .env
    python tools/check_accounts.py path/to/.env   # или явный файл
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Запуск как `python tools/check_accounts.py`: корень проекта в sys.path,
# иначе пакет app не найдётся (при `python -m tools.check_accounts` он и так есть).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_accounts, parse_chat_ids  # noqa: E402


def _mask(login: str) -> str:
    """first@example.com -> fi***@example.com — чтобы не светить логины в логах."""
    name, sep, domain = login.partition("@")
    if not sep:
        return (name[:2] + "***") if name else "***"
    shown = name[:2] if len(name) > 2 else name[:1]
    return f"{shown}***@{domain}"


def _load_env(env_file: Path) -> tuple:
    """Читает .env. Значения из файла приоритетнее окружения (для проверки важно
    видеть именно файл), недостающие ключи берутся из окружения.

    Возвращает (env, список ключей, заданных и в файле, и в окружении): у самого
    бота приоритет обратный — load_dotenv() не перезаписывает уже существующие
    переменные, поэтому «залипшие» export'ы в терминале могут подменять .env.
    """
    if not env_file.exists():
        load_dotenv()
        return dict(os.environ), []

    from_file = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    shadowed = sorted(k for k in from_file if k in os.environ and os.environ[k] != from_file[k])
    return {**os.environ, **from_file}, shadowed


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка кабинетов (мультиаккаунт)")
    parser.add_argument(
        "env_file",
        nargs="?",
        default=".env",
        help="путь к .env (по умолчанию .env)",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    env, shadowed = _load_env(env_file)
    print(f"Конфиг: {env_file if env_file.exists() else 'переменные окружения'}")
    if shadowed:
        print(
            "⚠️  эти переменные заданы и в .env, и в окружении терминала: "
            + ", ".join(shadowed)
        )
        print(
            "    здесь выигрывает файл, но сам бот (load_dotenv) файл НЕ перезаписывает — "
            "запускайте его в чистом терминале или сделайте `unset " + " ".join(shadowed) + "`"
        )

    try:
        accounts = load_accounts(env)
    except RuntimeError as exc:
        print(f"⛔ {exc}")
        return 1

    print(f"Кабинетов: {len(accounts)}")
    for account in accounts:
        state_path = Path(account.storage_state_path)
        mark = "сессия есть" if state_path.exists() else "сессии нет (потребуется вход)"
        print(f"  #{account.index} {account.name}: {_mask(account.login)} · {state_path} · {mark}")

    try:
        admins = parse_chat_ids(env)
        print(f"Админов Telegram: {len(admins)}")
    except RuntimeError as exc:
        print(f"⚠️  {exc}")

    backend = (env.get("WORKER_BACKEND") or "http").lower()
    print(f"WORKER_BACKEND={backend}")
    if backend == "browser" and len(accounts) > 1:
        print(
            f"⚠️  browser-бэкенд умеет только кабинет #1 — остальные "
            f"{len(accounts) - 1} не запустятся. Для нескольких сразу: WORKER_BACKEND=http"
        )

    raw_pollers = (env.get("TAKE_PARALLEL_POLLERS") or "1").strip()
    try:
        pollers = int(raw_pollers)
    except ValueError:
        print(f"⚠️  TAKE_PARALLEL_POLLERS={raw_pollers!r} — нужно целое число")
    else:
        print(
            f"TAKE_PARALLEL_POLLERS={pollers} → воронок на кабинет, "
            f"итого {pollers * len(accounts)} параллельных опросов очереди"
        )

    print("OK: конфигурация кабинетов читается" if accounts else "⛔ кабинетов не найдено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
