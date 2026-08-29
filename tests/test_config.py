from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from forgeloop.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_reads_key_from_environment(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "configured"}, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.api_key, "configured")
        self.assertEqual(settings.model, "deepseek-v4-pro")
        self.assertIsNone(settings.max_steps)

    def test_missing_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_repr_redacts_key(self) -> None:
        settings = Settings(api_key="secret")
        self.assertNotIn("secret", repr(settings))

    def test_settings_strip_values_and_require_https(self) -> None:
        settings = Settings(
            api_key="  secret  ",
            base_url=" https://api.deepseek.com/ ",
            model=" deepseek-v4-pro ",
        )
        self.assertEqual(settings.api_key, "secret")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        self.assertEqual(settings.model, "deepseek-v4-pro")
        with self.assertRaises(ConfigurationError):
            Settings(api_key="x", base_url="http://example.com")

    def test_localhost_http_is_allowed_for_local_compatible_gateway(self) -> None:
        settings = Settings(api_key="x", base_url="http://127.0.0.1:8000/v1")
        self.assertEqual(settings.base_url, "http://127.0.0.1:8000/v1")

    def test_non_positive_configured_steps_are_rejected(self) -> None:
        self.assertIsNone(Settings(api_key="x").max_steps)
        for invalid in (-1, 0):
            with self.subTest(max_steps=invalid), self.assertRaisesRegex(
                ConfigurationError,
                "at least 1",
            ):
                Settings(api_key="x", max_steps=invalid)


if __name__ == "__main__":
    unittest.main()
