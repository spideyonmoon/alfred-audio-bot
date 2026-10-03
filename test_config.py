import ast
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(os.environ, {}, clear=True):
    import config


class ConfigTests(unittest.TestCase):
    def test_ids_accept_signed_ids_spaces_and_duplicates(self):
        with patch.dict(os.environ, {"ALLOWED_CHATS": " -100123, 456, -100123 "}):
            self.assertEqual(config.env_ids("ALLOWED_CHATS"), {-100123, 456})

    def test_blank_lists_grant_no_access(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.env_ids("ALLOWED_CHATS"), set())
        with patch.dict(os.environ, {"ADMIN_IDS": "  "}):
            self.assertEqual(config.env_ids("ADMIN_IDS"), set())

    def test_bad_lists_fail_with_setting_name(self):
        for raw in ('{"-100123": [0, 42]}', '[123]', '123,', '123,abc'):
            with self.subTest(raw=raw), patch.dict(os.environ, {"ALLOWED_CHATS": raw}):
                with self.assertRaisesRegex(ValueError, "ALLOWED_CHATS"):
                    config.env_ids("ALLOWED_CHATS")

    def test_missing_credentials_and_invalid_api_id(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "BOT_TOKEN, API_ID, API_HASH"):
                config.telegram_credentials()
        for api_id in ("abc", "0", "-1"):
            with patch.dict(os.environ, {"BOT_TOKEN": "test", "API_HASH": "test", "API_ID": api_id}):
                with self.assertRaisesRegex(ValueError, "API_ID"):
                    config.telegram_credentials()

    def test_invalid_resource_settings_fail(self):
        for raw in ("0", "-2", "abc"):
            with patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": raw}):
                with self.assertRaisesRegex(ValueError, "MAX_CONCURRENT_JOBS"):
                    config.env_int("MAX_CONCURRENT_JOBS", 1)
        for raw in ("0", "-1", "nan", "inf", "abc"):
            with patch.dict(os.environ, {"PROGRESS_UPDATE_INTERVAL": raw}):
                with self.assertRaisesRegex(ValueError, "PROGRESS_UPDATE_INTERVAL"):
                    config.env_interval()

    def test_env_file_loaded_beside_module_and_overrides_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.py").write_text(Path(config.__file__).read_text(encoding="utf-8"), encoding="utf-8")
            (path / ".env").write_text(
                "BOT_TOKEN=test\nAPI_ID=123\nAPI_HASH=test\n"
                "ALLOWED_CHATS=-100123,456\nADMIN_IDS=789\n"
                "MAX_CONCURRENT_JOBS=2\nAF_EXTRACTORS=3\nPROGRESS_UPDATE_INTERVAL=12\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": "99"}, clear=True):
                settings = runpy.run_path(str(path / "config.py"))
                self.assertEqual(settings["MAX_CONCURRENT_JOBS"], 2)
                self.assertEqual(settings["PROGRESS_UPDATE_INTERVAL"], 12)
                self.assertEqual(settings["ALLOWED_CHATS"], {-100123, 456})
                self.assertEqual(settings["ADMIN_IDS"], {789})
                self.assertEqual(os.environ["AF_EXTRACTORS"], "3")
                self.assertEqual(settings["telegram_credentials"](), ("test", 123, "test"))

    def test_authorization_allows_chats_and_admins_without_topic_restrictions(self):
        # Exercise the actual auth function without creating a Telegram session.
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
        auth = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_check_auth")
        namespace = {"Message": object, "ALLOWED_CHATS": {-100123}, "ADMIN_IDS": {789}}
        exec(compile(ast.Module(body=[auth], type_ignores=[]), "bot.py", "exec"), namespace)
        for chat, user, topic, expected in (
            (-100123, 456, None, True), (-100123, 456, 42, True),
            (-100999, 456, None, False), (-100999, 789, None, True),
            (-100123, None, None, True), (-100999, None, None, False),
        ):
            with self.subTest(chat=chat, user=user, topic=topic):
                message = SimpleNamespace(chat=SimpleNamespace(id=chat),
                                          from_user=SimpleNamespace(id=user) if user else None,
                                          message_thread_id=topic)
                self.assertEqual(namespace["_check_auth"](message), expected)


if __name__ == "__main__":
    unittest.main()
