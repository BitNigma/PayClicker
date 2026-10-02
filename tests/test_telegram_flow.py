"""Диалоги выбора кабинета и маршрутизация 2FA в телеграм-боте (мультиаккаунт).

Запуск из корня проекта:
    python -m unittest discover -s tests -t .
"""
import asyncio
import unittest
from types import SimpleNamespace
from app.config import Account, Config
from app.state import state
from app.telegram_bot import (
    build_bot_app,
    cmd_run,
    cmd_run_all,
    cmd_status,
    cmd_stop,
    cmd_stop_all,
    cmd_watch,
    on_text_message,
)
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
class MultiAccountRunTest(DialogTestBase):
    """Несколько кабинетов запускаются одной командой: /run 1 3, /run all, /runall,
    а также выбором нескольких номеров в диалоге «какой кабинет?»."""

    def test_run_with_args_starts_several(self):
        cfg = make_config(3)
        update, message = make_update("/run 1 3")
        asyncio.run(cmd_run(update, SimpleNamespace(args=["1", "3"]), cfg))
        self.assertTrue(state.workers[1].running)
        self.assertTrue(state.workers[3].running)
        self.assertEqual(state.workers[1].mode, "take")
        self.assertEqual(state.workers[3].mode, "take")
        self.assertNotIn(2, state.workers)
        self.assertNotIn(CHAT_ID, state.chat_pending)
        self.assertIn("кабинетов: 2", message.replies[-1])

    def test_run_with_args_all_starts_every_account(self):
        cfg = make_config(3)
        update, message = make_update("/run all")
        asyncio.run(cmd_run(update, SimpleNamespace(args=["all"]), cfg))
        self.assertEqual(sorted(state.workers), [1, 2, 3])
        self.assertTrue(all(ws.running for ws in state.workers.values()))
        self.assertTrue(all(ws.mode == "take" for ws in state.workers.values()))

    def test_watch_and_stop_with_args_apply_to_listed_accounts(self):
        cfg = make_config(3)
        update, message = make_update("/watch 1;3")  # «;» как разделитель тоже понимаем
        asyncio.run(cmd_watch(update, SimpleNamespace(args=["1;3"]), cfg))
        self.assertTrue(state.workers[1].running)
        self.assertEqual(state.workers[1].mode, "watch")
        self.assertNotIn(2, state.workers)
        update, message = make_update("/stop 1 3")
        asyncio.run(cmd_stop(update, SimpleNamespace(args=["1", "3"]), cfg))
        self.assertFalse(state.workers[1].running)
        self.assertFalse(state.workers[3].running)

    def test_run_dialog_accepts_several_numbers(self):
        cfg = make_config(3)
        update, message = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        update, message = make_update("1 3")
        asyncio.run(on_text_message(update, None, cfg))
        self.assertTrue(state.workers[1].running)
        self.assertTrue(state.workers[3].running)
        self.assertEqual(state.workers[3].mode, "take")
        self.assertNotIn(CHAT_ID, state.chat_pending)

    def test_run_dialog_accepts_all_word(self):
        cfg = make_config(3)
        update, message = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        update, message = make_update("all")
        asyncio.run(on_text_message(update, None, cfg))
        self.assertEqual(sorted(state.workers), [1, 2, 3])
        self.assertTrue(all(ws.running for ws in state.workers.values()))

    def test_stop_all_command_stops_every_account(self):
        cfg = make_config(3)
        for i in (1, 2, 3):
            state.ensure(i, f"Кабинет {i}").running = True
        update, message = make_update("/stopall")
        asyncio.run(cmd_stop_all(update, None, cfg))
        self.assertFalse(any(ws.running for ws in state.workers.values()))
        stopped = [ln for ln in message.replies[-1].splitlines() if "остановлен" in ln]
        self.assertEqual(len(stopped), 3)

    def test_runall_command_starts_every_account(self):
        cfg = make_config(3)
        update, message = make_update("/runall")
        asyncio.run(cmd_run_all(update, None, cfg))
        self.assertEqual(sorted(state.workers), [1, 2, 3])
        self.assertTrue(all(ws.running for ws in state.workers.values()))

    def test_unknown_number_in_list_keeps_dialog_open(self):
        cfg = make_config(3)
        update, message = make_update("/run")
        asyncio.run(cmd_run(update, None, cfg))
        update, message = make_update("1 9")
        asyncio.run(on_text_message(update, None, cfg))
        self.assertIn("Не понял номер кабинета", message.replies[-1])
        self.assertEqual(state.chat_pending[CHAT_ID]["step"], "choose_account")
        self.assertFalse(state.workers.get(1) and state.workers[1].running)

    def test_unknown_number_in_command_args_starts_nothing(self):
        cfg = make_config(3)
        update, message = make_update("/run 7")
        asyncio.run(cmd_run(update, SimpleNamespace(args=["7"]), cfg))
        self.assertEqual(state.workers, {})
        self.assertIn("Не понял номера кабинетов", message.replies[-1])

    def test_status_summarizes_running_cabinets(self):
        cfg = make_config(3)
        state.ensure(1, "Кабинет 1").running = True
        state.ensure(3, "Кабинет 3").running = True
        update, message = make_update("/status")
        asyncio.run(cmd_status(update, None, cfg))
        self.assertIn("работает сейчас: 2 из 3", message.replies[-1])



class BotCommandWiringTest(DialogTestBase):
    """Команды мультиаккаунта действительно подключены к боту (сборка app без сети)."""

    def test_multiaccount_commands_are_registered(self):
        cfg = make_config(2)
        app = build_bot_app(cfg)
        commands = set()
        for group in app.handlers.values():
            for handler in group:
                commands.update(getattr(handler, "commands", None) or set())
        for name in (
            "run",
            "stop",
            "watch",
            "runall",
            "watchall",
            "stopall",
            "status",
            "snapshot",
            "id",
        ):
            self.assertIn(name, commands)

