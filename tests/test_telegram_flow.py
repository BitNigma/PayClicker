"""Диалоги выбора кабинета и маршрутизация 2FA в телеграм-боте (мультиаккаунт).

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import unittest
from types import SimpleNamespace
from app.config import Account, Config
from app.state import state
from app.telegram_bot import cmd_run, cmd_status, cmd_stop, on_text_message
CHAT_ID = 3036948

def make_config(accounts_count: int = 2) -> Config:
    accounts = [Account(index=i,
      name=f"Кабинет {i}",
      login=f"u{i}@example.com",
      password=f"pw{i}",
      storage_state_path=("/tmp/auth.json" if i == 1 else f"/tmp/auth_{i}.json")) for i in range(1, accounts_count + 1)]
    return Config(telegram_token="t",
      allowed_chat_ids=[
     CHAT_ID],
      accounts=accounts,
      site_url="https://x/signin",
      queue_url="https://x/queue",
      demands_url="https://x/demands",
      target_queue_size=10,
      reload_min_ms=1,
      reload_max_ms=2,
      take_poll_min_ms=1,
      take_poll_max_ms=2,
      debug_capture_attempts=False,
      headless=False,
      log_path="/tmp/actions.log",
      http_timeout_s=1.0,
      metrics_path="/tmp/metrics.jsonl",
      worker_backend="http",
      take_parallel_pollers=1)


class FakeMessage:

    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def make_update(text, chat_id=CHAT_ID):
    """Минимальный Update-двойник: effective_chat + message.reply_text()."""
    message = FakeMessage(text)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
      message=message)
    return (
     update, message)


class DialogTestBase(unittest.TestCase):

    def setUp(self):
        state.workers.clear()
        state.chat_pending.clear()


class RunStopDialogTest(DialogTestBase):

    def test_run_asks_which_account_then_starts_chosen(self):
        cfg = make_config(2)
        update, message = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        self.assertIn("Какой кабинет", message.replies[-1])
        self.assertEqual(state.chat_pending[CHAT_ID]["step"], "choose_account")
        update, message = make_update("2")
        asyncio.run(on_text_message(update, None, cfg))
        ws2 = state.workers[2]
        self.assertTrue(ws2.running)
        self.assertEqual(ws2.mode, "take")
        self.assertNotIn(1, state.workers)
        self.assertNotIn(CHAT_ID, state.chat_pending)

    def test_run_with_single_account_starts_immediately(self):
        cfg = make_config(1)
        update, message = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        self.assertTrue(state.workers[1].running)
        self.assertEqual(state.workers[1].mode, "take")
        self.assertNotIn(CHAT_ID, state.chat_pending)

    def test_stop_stops_only_chosen_account(self):
        cfg = make_config(2)
        state.ensure(1, "Кабинет 1").running = True
        state.ensure(2, "Кабинет 2").running = True
        update, message = make_update("/stop")
        asyncio.run(cmd_stop(update, None, cfg))
        self.assertEqual(state.chat_pending[CHAT_ID]["step"], "choose_account")
        update, message = make_update("1")
        asyncio.run(on_text_message(update, None, cfg))
        self.assertFalse(state.workers[1].running)
        self.assertTrue(state.workers[2].running)

    def test_invalid_account_number_keeps_dialog_open(self):
        cfg = make_config(2)
        update, _ = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        update, message = make_update("9")
        asyncio.run(on_text_message(update, None, cfg))
        self.assertIn("Не понял номер кабинета", message.replies[-1])
        self.assertEqual(state.chat_pending[CHAT_ID]["step"], "choose_account")

    def test_status_lists_all_accounts(self):
        cfg = make_config(2)
        state.ensure(1, "Кабинет 1").running = True
        update, message = make_update("/status")
        asyncio.run(cmd_status(update, None, cfg))
        text = message.replies[-1]
        self.assertIn("Кабинетов: 2", text)
        self.assertIn("Кабинет 1", text)
        self.assertIn("Кабинет 2", text)
