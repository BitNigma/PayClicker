from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Account:
    """Один кабинет (аккаунт) сайта: свои логин/пароль и свой файл сессии.

    index — 1-based номер кабинета, которым оперируют команды /run и /stop.
    """

    index: int
    name: str
    login: str
    password: str
    storage_state_path: str


@dataclass
class Config:
    telegram_token: str
    # id Telegram-админов, которым разрешено управлять ботом (минимум один)
    allowed_chat_ids: list[int]

    # Кабинеты (аккаунты). Минимум один; кабинет #1 — основной.
    accounts: list[Account]

    site_url: str
    queue_url: str
    demands_url: str

    target_queue_size: int
    reload_min_ms: int
    reload_max_ms: int
    take_poll_min_ms: int
    take_poll_max_ms: int
    debug_capture_attempts: bool
    headless: bool

    log_path: str

    # HTTP-горячий цикл (http_worker, без браузера)
    http_timeout_s: float
    metrics_path: str
    worker_backend: str
    take_parallel_pollers: int
    # как часто (сек) сверяемся со страницей demands: сколько заявок в работе
    demands_sync_s: int = 10

    @property
    def allowed_chat_id(self) -> int:
        """Первый (основной) админ — для обратной совместимости и логов."""
        return self.allowed_chat_ids[0]

    @property
    def site_login(self) -> str:
        return self.accounts[0].login

    @property
    def site_password(self) -> str:
        return self.accounts[0].password

    @property
    def storage_state_path(self) -> str:
        return self.accounts[0].storage_state_path


# Любая из этих переменных окружения может содержать id админов (можно несколько).
_ADMIN_ID_ENV_VARS = (
    "TELEGRAM_ALLOWED_CHAT_ID",   # исторически одиночный, теперь допускает список
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ADMIN_IDS",
    "TELEGRAM_SUB_USERS",
    "SUB_USER",
    "SUB_USER_ID",
    "SUB_USERS",
)
_ID_SPLIT_RE = re.compile(r"[,\s;]+")


def parse_chat_ids(env) -> list[int]:
    """Собирает id Telegram-админов из нескольких переменных окружения.

    Любая из поддерживаемых переменных может содержать НЕСКОЛЬКО id, разделённых
    запятыми/точками с запятой/пробелами (например "111, 222 333"). Дубликаты
    убираются, порядок сохраняется. Если не задан ни один id — ошибка.
    """
    ids: list[int] = []
    for key in _ADMIN_ID_ENV_VARS:
        raw = env.get(key)
        if not raw:
            continue
        for part in _ID_SPLIT_RE.split(raw):
            part = part.strip()
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                raise RuntimeError(
                    f"Некорректный chat_id «{part}» в {key}: ожидаются числа, "
                    f"разделённые запятыми/пробелами"
                )
            chat_id = int(part)
            if chat_id not in ids:
                ids.append(chat_id)
    if not ids:
        raise RuntimeError(
            "Не задан ни один админ: укажи TELEGRAM_ALLOWED_CHAT_ID "
            "(можно несколько id через запятую)"
        )
    return ids


def _req(env, name: str) -> str:
    val = env.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def load_accounts(env) -> list[Account]:
    """Собирает список кабинетов из переменных окружения.

    Кабинет #1:      SITE_LOGIN / SITE_PASSWORD / [SITE_NAME]        (файл сессии STORAGE_STATE_PATH)
    Кабинет N (N>=2): SITE_LOGIN_N / SITE_PASSWORD_N / [SITE_NAME_N] (файл сессии auth_N.json рядом)
    Кабинеты перебираются, пока заданы SITE_LOGIN_N/SITE_PASSWORD_N.
    """
    base_state = env.get("STORAGE_STATE_PATH", "/data/auth.json")
    base_dir = Path(base_state).parent

    accounts = [
        Account(
            index=1,
            name=env.get("SITE_NAME", "Кабинет 1"),
            login=_req(env, "SITE_LOGIN"),
            password=_req(env, "SITE_PASSWORD"),
            storage_state_path=base_state,
        )
    ]

    n = 2
    while True:
        login = env.get(f"SITE_LOGIN_{n}")
        password = env.get(f"SITE_PASSWORD_{n}")
        if not login and not password:
            break
        if not login or not password:
            raise RuntimeError(
                f"Для кабинета {n} нужно задать И SITE_LOGIN_{n}, И SITE_PASSWORD_{n}"
            )
        accounts.append(
            Account(
                index=n,
                name=env.get(f"SITE_NAME_{n}", f"Кабинет {n}"),
                login=login,
                password=password,
                storage_state_path=str(base_dir / f"auth_{n}.json"),
            )
        )
        n += 1

    return accounts


def load_config() -> Config:
    env = os.environ

    log_path = env.get("LOG_PATH", "/data/actions.log")
    metrics_default = str(Path(log_path).parent / "metrics.jsonl")

    return Config(
        telegram_token=_req(env, "TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=parse_chat_ids(env),
        accounts=load_accounts(env),
        site_url=_req(env, "SITE_URL"),
        queue_url=_req(env, "QUEUE_URL"),
        demands_url=_req(env, "DEMANDS_URL"),
        target_queue_size=int(env.get("TARGET_QUEUE_SIZE", "10")),
        reload_min_ms=int(env.get("RELOAD_MIN_MS", "2000")),
        reload_max_ms=int(env.get("RELOAD_MAX_MS", "3000")),
        take_poll_min_ms=int(env.get("TAKE_POLL_MIN_MS", "500")),
        take_poll_max_ms=int(env.get("TAKE_POLL_MAX_MS", "900")),
        debug_capture_attempts=env.get("DEBUG_CAPTURE_ATTEMPTS", "true").lower() == "true",
        headless=env.get("HEADLESS", "false").lower() == "true",
        log_path=log_path,
        http_timeout_s=float(env.get("HTTP_TIMEOUT_S", "10")),
        metrics_path=env.get("METRICS_PATH", metrics_default),
        # "http" — горячий цикл на httpx (по умолчанию); "browser" — старый Playwright-воркер
        worker_backend=env.get("WORKER_BACKEND", "http").lower(),
        # сколько независимых воронок опрашивают очередь одновременно (1 = одна)
        take_parallel_pollers=int(env.get("TAKE_PARALLEL_POLLERS", "1")),
        # как часто (сек) сверяемся со страницей demands: истина о занятых слотах
        demands_sync_s=max(1, int(env.get("DEMANDS_SYNC_S", "10"))),
    )

