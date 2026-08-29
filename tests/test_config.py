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

    def test_missing_key_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_repr_redacts_key(self) -> None:
        settings = Settings(api_key="secret")
        self.assertNotIn("secret", repr(settings))


if __name__ == "__main__":
    unittest.main()
