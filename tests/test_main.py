"""Тесты точки входа: устойчивость запуска и разбор конфликта двух экземпляров.

Повод: сервис «падал» по двум причинам, обе выглядят как крэш в терминале —
1) запуск файлом (`python app/main.py`) давал ImportError из-за относительных
   импортов (нет известного родительского пакета);
2) второй запущенный экземпляр бота получал 409 Conflict от Telegram, и
   telegram.ext печатал многострочный traceback «Exception happened while
   polling», хотя процесс оставался жив.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram.error import Conflict

from app import main as main_module

ROOT = Path(__file__).resolve().parents[1]


class PollingErrorLoggingTest(unittest.TestCase):
    """409 Conflict должен объясняться одной строкой, а не трейсбеком."""

    def test_conflict_is_explained_without_traceback(self):
        with self.assertLogs("main", level="ERROR") as captured:
            main_module._on_polling_error(
                Conflict("Conflict: terminated by other getUpdates request")
            )
        text = "\n".join(captured.output)
        self.assertIn("НЕ крэш", text)
        self.assertIn("ДРУГОЙ экземпляр", text)
        self.assertNotIn("Traceback", text)
        # Ровно одна запись на событие — иначе лог снова зашумится.
        self.assertEqual(len(captured.records), 1)

    def test_other_errors_keep_generic_wording(self):
        with self.assertLogs("main", level="ERROR") as captured:
            main_module._on_polling_error(RuntimeError("boom"))
        text = "\n".join(captured.output)
        self.assertIn("RuntimeError", text)
        self.assertIn("boom", text)
        self.assertEqual(len(captured.records), 1)


class PickWorkerTest(unittest.TestCase):
    """WORKER_BACKEND по умолчанию — http; browser остаётся как откат."""

    def test_http_is_default_backend(self):
        env = {k: v for k, v in main_module.os.environ.items() if k != "WORKER_BACKEND"}
        with patch.dict(main_module.os.environ, env, clear=True):
            run_worker, backend = main_module._pick_worker()
        self.assertEqual(backend, "http")
        self.assertEqual(run_worker.__module__, "app.http_worker")

    def test_browser_backend_can_be_selected(self):
        with patch.dict(main_module.os.environ, {"WORKER_BACKEND": "browser"}):
            run_worker, backend = main_module._pick_worker()
        self.assertEqual(backend, "browser")
        self.assertEqual(run_worker.__module__, "app.browser_worker")


class SelfCheckTest(unittest.TestCase):
    """--check проверяет конфиг без сети и без запуска бота."""

    def test_returns_zero_and_logs_summary(self):
        cfg = SimpleNamespace(
            accounts=[SimpleNamespace(index=1, name="Кабинет 1")],
            allowed_chat_ids=[111],
            proxy_urls=["http://127.0.0.1:8080"],
            target_queue_size=8,
            headless=True,
        )
        with patch.object(main_module, "load_config", return_value=cfg), patch.object(
            main_module, "_pick_worker", return_value=(lambda: None, "http")
        ), self.assertLogs("main", level="INFO") as captured:
            rc = main_module._run_self_check()
        self.assertEqual(rc, 0)
        text = "\n".join(captured.output)
        self.assertIn("Проверка OK", text)
        self.assertIn("кабинетов=1", text)
        self.assertIn("прокси=1", text)


class ShadowedEnvTest(unittest.TestCase):
    """`export TARGET_QUEUE_SIZE=10` в терминале тихо перекрывает .env (8).

    load_dotenv() не перезаписывает существующие переменные, поэтому бот брал
    значение из окружения — и писал «цель 10 заявок» при TARGET_QUEUE_SIZE=8.
    Теперь при старте выводится предупреждение со списком таких переменных.
    """

    def _env_file(self, tmp: Path, text: str) -> Path:
        path = tmp / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_detects_value_shadowed_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = self._env_file(Path(tmp), "TARGET_QUEUE_SIZE=8\nHEADLESS=true\n")
            with patch.dict(
                main_module.os.environ, {"TARGET_QUEUE_SIZE": "10"}, clear=False
            ):
                shadowed = main_module._shadowed_env(env_file)
        self.assertEqual(shadowed["TARGET_QUEUE_SIZE"], ("10", "8"))
        self.assertNotIn("HEADLESS", shadowed)  # в окружении нет — не перекрывает

    def test_same_value_is_not_shadowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = self._env_file(Path(tmp), "TARGET_QUEUE_SIZE=8\n")
            with patch.dict(
                main_module.os.environ, {"TARGET_QUEUE_SIZE": "8"}, clear=False
            ):
                self.assertEqual(main_module._shadowed_env(env_file), {})

    def test_warning_lists_keys_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = self._env_file(
                Path(tmp), "TARGET_QUEUE_SIZE=8\nSITE_PASSWORD=fromfile\n"
            )
            with patch.dict(
                main_module.os.environ,
                {"TARGET_QUEUE_SIZE": "10", "SITE_PASSWORD": "fromshell"},
                clear=False,
            ), self.assertLogs("main", level="WARNING") as captured:
                main_module._warn_shadowed_env(env_file)
        text = "\n".join(captured.output)
        self.assertIn("TARGET_QUEUE_SIZE", text)
        self.assertIn("'10'", text)
        self.assertIn("'8'", text)
        self.assertIn("SITE_PASSWORD", text)
        self.assertIn("скрыты", text)
        self.assertNotIn("fromshell", text)
        self.assertNotIn("fromfile", text)


class DirectScriptRunTest(unittest.TestCase):
    """Регрессия: `python app/main.py` не должен падать с ImportError."""

    def test_direct_script_run_resolves_relative_imports(self):
        if not (ROOT / ".env").exists():
            self.skipTest(".env отсутствует — прямой запуск без конфига не проверить")
        proc = subprocess.run(
            [sys.executable, "app/main.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, msg=output[-2000:])
        self.assertIn("Проверка OK", output)
        self.assertNotIn("ImportError", output)
        self.assertNotIn("attempted relative import", output)


if __name__ == "__main__":
    unittest.main()
