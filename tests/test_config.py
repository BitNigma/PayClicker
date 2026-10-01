"""Тесты парсинга id Telegram-админов (мультиадминный режим)."""
import unittest

from app.config import _ADMIN_ID_ENV_VARS, load_accounts, parse_chat_ids


class ParseChatIdsTest(unittest.TestCase):
    def test_single_id_backward_compatible(self):
        self.assertEqual(parse_chat_ids({"TELEGRAM_ALLOWED_CHAT_ID": "3036948"}), [3036948])

    def test_multiple_ids_comma_and_space(self):
        env = {"TELEGRAM_ALLOWED_CHAT_ID": "1, 2 3;4\n5"}
        self.assertEqual(parse_chat_ids(env), [1, 2, 3, 4, 5])

    def test_merges_several_env_vars_and_dedupes(self):
        env = {
            "TELEGRAM_ALLOWED_CHAT_ID": "100,200",
            "TELEGRAM_ADMIN_IDS": "200,300",
            "SUB_USER": "400",
        }
        self.assertEqual(parse_chat_ids(env), [100, 200, 300, 400])

    def test_all_supported_names_are_considered(self):
        env = {name: str(1000 + i) for i, name in enumerate(_ADMIN_ID_ENV_VARS)}
        ids = parse_chat_ids(env)
        self.assertEqual(ids, [1000 + i for i in range(len(_ADMIN_ID_ENV_VARS))])

    def test_negative_and_empty_parts(self):
        self.assertEqual(parse_chat_ids({"SUB_USERS": " -100500 , ,7 "}), [-100500, 7])

    def test_invalid_value_raises(self):
        with self.assertRaises(RuntimeError):
            parse_chat_ids({"TELEGRAM_ALLOWED_CHAT_ID": "abc"})

    def test_no_admin_raises(self):
        with self.assertRaises(RuntimeError):
            parse_chat_ids({})

    def test_blank_values_are_ignored(self):
        with self.assertRaises(RuntimeError):
            parse_chat_ids({"TELEGRAM_ALLOWED_CHAT_ID": "  ", "TELEGRAM_ADMIN_IDS": ""})


class LoadAccountsTest(unittest.TestCase):
    """Мультиаккаунт: кабинет #1 из SITE_LOGIN/SITE_PASSWORD, кабинеты N>=2 из SITE_LOGIN_N..."""

    BASE_ENV = {
        "SITE_LOGIN": "one@example.com",
        "SITE_PASSWORD": "pw1",
        "SITE_NAME": "Основной",
        "STORAGE_STATE_PATH": "/data/auth.json",
    }

    def test_single_account_from_base_vars(self):
        accounts = load_accounts(dict(self.BASE_ENV))
        self.assertEqual(len(accounts), 1)
        a = accounts[0]
        self.assertEqual((a.index, a.name, a.login, a.password), (1, "Основной", "one@example.com", "pw1"))
        self.assertEqual(a.storage_state_path, "/data/auth.json")

    def test_single_account_default_name(self):
        env = {k: v for k, v in self.BASE_ENV.items() if k != "SITE_NAME"}
        self.assertEqual(load_accounts(env)[0].name, "Кабинет 1")

    def test_second_account_from_suffixed_vars(self):
        env = dict(
            self.BASE_ENV,
            SITE_LOGIN_2="two@example.com",
            SITE_PASSWORD_2="pw2",
            SITE_NAME_2="Второй",
        )
        accounts = load_accounts(env)
        self.assertEqual(len(accounts), 2)
        a = accounts[1]
        self.assertEqual((a.index, a.name, a.login), (2, "Второй", "two@example.com"))
        # отдельный файл сессии рядом с первым
        self.assertEqual(a.storage_state_path, "/data/auth_2.json")

    def test_third_account_sequential_numbering(self):
        env = dict(
            self.BASE_ENV,
            SITE_LOGIN_2="a@b.c",
            SITE_PASSWORD_2="p2",
            SITE_LOGIN_3="c@d.e",
            SITE_PASSWORD_3="p3",
        )
        accounts = load_accounts(env)
        self.assertEqual([a.index for a in accounts], [1, 2, 3])
        self.assertEqual(accounts[2].storage_state_path, "/data/auth_3.json")
        self.assertEqual(accounts[2].name, "Кабинет 3")

    def test_second_account_without_name_gets_default(self):
        env = dict(self.BASE_ENV, SITE_LOGIN_2="two@example.com", SITE_PASSWORD_2="pw2")
        self.assertEqual(load_accounts(env)[1].name, "Кабинет 2")

    def test_partial_second_account_raises(self):
        env = dict(self.BASE_ENV, SITE_LOGIN_2="two@example.com")
        with self.assertRaises(RuntimeError):
            load_accounts(env)

    def test_missing_base_login_raises(self):
        with self.assertRaises(RuntimeError):
            load_accounts({"SITE_PASSWORD": "pw"})


if __name__ == "__main__":
    unittest.main()
