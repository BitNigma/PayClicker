"""Тесты антибот-защиты HTTP-цикла: 429/403, темп запросов, ложная реавторизация.

Повод: в режиме run бот опрашивал очередь ~20 раз/с (2 воронки × TAKE_POLL_MS=60),
сайт отвечал 429, и КАЖДЫЙ 429 удалял auth.json и уводил в браузерную
реавторизацию — получался вечный цикл «антибот → сброс cookies → антибот».
В режиме watch запросов в разы меньше, поэтому там всё работало.

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from app.config import Account, Config
from app.http_worker import (
    BlockedError,
    Metrics,
    PayoutClient,
    RateLimited,
    _confirm_taken,
    _poll_loop,
    _retry_after_seconds,
    _session_ok,
    watch_loop,
)
from app.state import WorkerState


def make_cfg(**overrides) -> Config:
    """Минимальный Config для тестов темпа/антибота."""
    base = dict(
        telegram_token="t",
        allowed_chat_ids=[1],
        accounts=[
            Account(
                index=1,
                name="Кабинет 1",
                login="a@b.c",
                password="p",
                storage_state_path="/tmp/auth-test.json",
            )
        ],
        site_url="https://x/signin",
        queue_url="https://x/queue",
        demands_url="https://x/demands",
        target_queue_size=10,
        reload_min_ms=10,
        reload_max_ms=20,
        take_poll_min_ms=10,
        take_poll_max_ms=20,
        debug_capture_attempts=False,
        headless=True,
        log_path="/tmp/actions-test.log",
        http_timeout_s=5.0,
        metrics_path="/tmp/metrics-test.jsonl",
        worker_backend="http",
        take_parallel_pollers=1,
    )
    base.update(overrides)
    return Config(**base)


def _resp(status: int, headers: dict | None = None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        text=text,
        request=httpx.Request("GET", "https://x/queue"),
    )


def _client(cfg: Config) -> PayoutClient:
    return PayoutClient(cfg, {})


def _close(client: PayoutClient) -> None:
    asyncio.run(client.aclose())


class RetryAfterTest(unittest.TestCase):
    """Заголовок Retry-After: секунды или HTTP-дата (иначе None)."""

    def test_seconds(self):
        self.assertEqual(_retry_after_seconds(_resp(429, {"retry-after": "7"})), 7.0)

    def test_http_date(self):
        moment = datetime.now(timezone.utc) + timedelta(seconds=30)
        raw = format_datetime(moment, usegmt=True)
        value = _retry_after_seconds(_resp(429, {"retry-after": raw}))
        self.assertIsNotNone(value)
        self.assertTrue(25 <= value <= 31, value)

    def test_missing_or_garbage(self):
        self.assertIsNone(_retry_after_seconds(_resp(429)))
        self.assertIsNone(_retry_after_seconds(_resp(429, {"retry-after": "n/a"})))


class ClientCheckTest(unittest.TestCase):
    """429 → RateLimited (сессия жива), 403 → BlockedError, 200 → поощрение темпа."""

    def test_429_is_rate_limited_not_blocked(self):
        client = _client(make_cfg())
        try:
            with self.assertRaises(RateLimited) as ctx:
                client._check(_resp(429, {"retry-after": "2"}))
            self.assertEqual(ctx.exception.status, 429)
            self.assertAlmostEqual(ctx.exception.retry_after, 2.0, places=3)
            # После лимита интервал вырос (замедление), пауза учтена.
            self.assertGreaterEqual(client._interval, 1.0)
        finally:
            _close(client)

    def test_503_is_rate_limited(self):
        client = _client(make_cfg())
        try:
            with self.assertRaises(RateLimited):
                client._check(_resp(503))
        finally:
            _close(client)

    def test_403_is_blocked(self):
        client = _client(make_cfg())
        try:
            with self.assertRaises(BlockedError):
                client._check(_resp(403))
        finally:
            _close(client)

    def test_challenge_page_is_blocked(self):
        client = _client(make_cfg())
        try:
            with self.assertRaises(BlockedError):
                client._check(_resp(200, text="<html>Just a moment...</html>"))
        finally:
            _close(client)

    def test_success_speeds_up_after_streak(self):
        client = _client(make_cfg())
        try:
            client._interval = 4.0
            client._ok_streak = 9
            client._check(_resp(200, text="<html><body>ok</body></html>"))
            self.assertLess(client._interval, 4.0)
        finally:
            _close(client)


class ThrottleTest(unittest.TestCase):
    """Общий темп клиента: запросы не «складываются», пауза после 429 соблюдается."""

    def test_min_interval_spaces_calls(self):
        client = _client(make_cfg(http_min_interval_ms=120))
        try:
            async def run() -> float:
                await client._throttle()  # первая — сразу
                start = time.perf_counter()
                await client._throttle()
                return time.perf_counter() - start

            gap = asyncio.run(run())
            self.assertGreaterEqual(gap, 0.09, f"пауза {gap:.3f} с")
        finally:
            _close(client)

    def test_penalty_sets_cooldown(self):
        client = _client(make_cfg())
        try:
            delay = client._penalize(0.2)

            async def run() -> float:
                start = time.perf_counter()
                await client._throttle()
                return time.perf_counter() - start

            self.assertAlmostEqual(delay, 0.2, places=3)
            self.assertGreaterEqual(asyncio.run(run()), 0.19)
        finally:
            _close(client)


class _FakeClient:
    """Подмена PayoutClient: отдаёт заранее заданные ответы/исключения."""

    def __init__(self, cfg: Config, responses: list):
        self.cfg = cfg
        self._responses = list(responses)
        self.calls = 0

    async def fetch_queue(self) -> str:
        self.calls += 1
        item = self._responses.pop(0) if self._responses else ""
        if isinstance(item, Exception):
            raise item
        return item


def _ws(mode: str = "take") -> WorkerState:
    return WorkerState(index=1, name="Кабинет 1", running=True, mode=mode)


async def _run_short(coro, seconds: float = 0.5) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _noop(text: str) -> None:
    return None


async def _noop_file(path: str, caption: str = "") -> None:
    return None


class PollLoopRateLimitTest(unittest.TestCase):
    """429 в горячем цикле: пауза и продолжение, без сброса cookies/реавторизации."""

    def setUp(self):
        from app.http_worker import _RATE_NOTICE_AT

        _RATE_NOTICE_AT.clear()

    def test_429_does_not_trigger_reauth(self):
        cfg = make_cfg()
        client = _FakeClient(cfg, [RateLimited(retry_after=0.05), "<html></html>"])
        ws = _ws()
        messages: list[str] = []

        async def notify(text: str) -> None:
            messages.append(text)

        asyncio.run(
            _run_short(
                _poll_loop(
                    client, cfg, cfg.accounts[0], ws, notify, _noop_file,
                    Metrics(cfg), set(), set(),
                )
            )
        )
        self.assertTrue(ws.running, "цикл не должен останавливаться на 429")
        self.assertFalse(ws.needs_reauth, "429 — не повод сбрасывать cookies")
        self.assertTrue(any("ограничил частоту" in m for m in messages), messages)

    def test_block_is_tolerated_before_reauth(self):
        cfg = make_cfg(http_block_retries=3, http_block_backoff_s=0.01)
        client = _FakeClient(cfg, [BlockedError("challenge"), "<html></html>"])
        ws = _ws()

        asyncio.run(
            _run_short(
                _poll_loop(
                    client, cfg, cfg.accounts[0], ws, _noop, _noop_file,
                    Metrics(cfg), set(), set(),
                )
            )
        )
        self.assertTrue(ws.running, "первый 403 терпим паузой, а не остановкой")
        self.assertFalse(ws.needs_reauth)
        self.assertGreaterEqual(client.calls, 2, "после паузы должен быть повторный GET")


class WatchLoopRateLimitTest(unittest.TestCase):
    """Watch-режим: 429 не останавливает наблюдение и не сбрасывает сессию."""

    def test_429_keeps_watching(self):
        cfg = make_cfg()
        client = _FakeClient(
            cfg, [RateLimited(retry_after=0.05), "<html></html>", "<html></html>"]
        )
        ws = _ws(mode="watch")
        asyncio.run(
            _run_short(
                watch_loop(client, cfg, cfg.accounts[0], ws, _noop, _noop_file, Metrics(cfg))
            )
        )
        self.assertTrue(ws.running)
        self.assertFalse(ws.needs_reauth)
        self.assertGreaterEqual(client.calls, 2)


class SessionCheckTest(unittest.TestCase):
    """_session_ok: 429 не должен выглядеть как «сессия протухла»."""

    def test_429_is_not_session_expired(self):
        class Client:
            async def fetch_queue(self):
                raise RateLimited(retry_after=0.01)

        self.assertTrue(asyncio.run(_session_ok(Client())))

    def test_signin_is_session_expired(self):
        from app.http_worker import SessionExpired

        class Client:
            async def fetch_queue(self):
                raise SessionExpired("redirect")

        self.assertFalse(asyncio.run(_session_ok(Client())))


class ConfirmRateLimitTest(unittest.TestCase):
    """Подтверждение взятия не падает трейсбеком на 429 — ждёт и повторяет."""

    HTML = (
        "<table><tr class='payout-table'>"
        "<td><input type='hidden' name='payout_id' value='123'></td>"
        "<td>в работе у провайдера</td></tr></table>"
    )

    def test_confirm_survives_rate_limit(self):
        cfg = make_cfg()

        class Client:
            def __init__(self):
                self.calls = 0

            async def fetch_demands(self):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimited(retry_after=0.01)
                return ConfirmRateLimitTest.HTML

        client = Client()
        ok = asyncio.run(_confirm_taken(client, cfg, "123", attempts=2, delay=0.01))
        self.assertTrue(ok)
        self.assertEqual(client.calls, 2)


class RateNoticeToggleTest(unittest.TestCase):
    """NOTIFY_RATE_LIMIT / RATE_NOTICE_GAP_S: уведомления о лимитах управляются из .env."""

    def setUp(self):
        from app.http_worker import _RATE_NOTICE_AT

        _RATE_NOTICE_AT.clear()

    def _run_poll(self, cfg: Config) -> list[str]:
        client = _FakeClient(cfg, [RateLimited(retry_after=0.01), "<html></html>"])
        ws = _ws()
        messages: list[str] = []

        async def notify(text: str) -> None:
            messages.append(text)

        asyncio.run(
            _run_short(
                _poll_loop(
                    client, cfg, cfg.accounts[0], ws, notify, _noop_file,
                    Metrics(cfg), set(), set(),
                ),
                seconds=0.3,
            )
        )
        return messages

    def test_notifications_can_be_disabled_from_env(self):
        self.assertEqual(self._run_poll(make_cfg(notify_rate_limit=False)), [])

    def test_notifications_are_sent_by_default(self):
        messages = self._run_poll(make_cfg())
        self.assertTrue(any("ограничил частоту" in m for m in messages), messages)

    def test_gap_suppresses_repeats(self):
        cfg = make_cfg(rate_notice_gap_s=3600)
        client = _FakeClient(cfg, [RateLimited(retry_after=0.01)] * 5)
        ws = _ws()
        messages: list[str] = []

        async def notify(text: str) -> None:
            messages.append(text)

        asyncio.run(
            _run_short(
                _poll_loop(
                    client, cfg, cfg.accounts[0], ws, notify, _noop_file,
                    Metrics(cfg), set(), set(),
                ),
                seconds=0.3,
            )
        )
        rate_messages = [m for m in messages if "ограничил частоту" in m]
        self.assertEqual(len(rate_messages), 1, messages)


class RateNoticeConfigTest(unittest.TestCase):
    """Поля конфига: NOTIFY_RATE_LIMIT (по умолчанию true) и RATE_NOTICE_GAP_S."""

    BASE_ENV = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_ALLOWED_CHAT_ID": "1",
        "SITE_LOGIN": "a@b.c",
        "SITE_PASSWORD": "p",
        "SITE_URL": "https://x/signin",
        "QUEUE_URL": "https://x/queue",
        "DEMANDS_URL": "https://x/demands",
    }

    def test_defaults(self):
        from unittest.mock import patch

        from app.config import load_config

        with patch.dict(os.environ, self.BASE_ENV, clear=True):
            cfg = load_config()
        self.assertTrue(cfg.notify_rate_limit)
        self.assertEqual(cfg.rate_notice_gap_s, 30.0)

    def test_disabled_and_custom_gap(self):
        from unittest.mock import patch

        from app.config import load_config

        env = dict(self.BASE_ENV, NOTIFY_RATE_LIMIT="false", RATE_NOTICE_GAP_S="600")
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertFalse(cfg.notify_rate_limit)
        self.assertEqual(cfg.rate_notice_gap_s, 600.0)


if __name__ == "__main__":
    unittest.main()
