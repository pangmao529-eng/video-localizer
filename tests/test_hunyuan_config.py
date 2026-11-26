import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_localizer import config


class HunyuanConfigurationTests(unittest.TestCase):
    def test_save_persists_nonsecret_settings_and_vault_key_separately(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "app_dir", return_value=Path(directory)), \
                patch.object(config, "_store_key") as save_key:
            result = config._save_configuration({"base_url": "https://tokenhub.tencentmaas.com/v1/",
                                                 "model": "hy4", "key": "secret-for-mocked-vault"})
            self.assertEqual(config.settings(), {"provider": "hunyuan", "base_url": "https://tokenhub.tencentmaas.com/v1", "model": "hy4"})
            self.assertEqual(result, {"configured": True, "provider": "hunyuan", "model": "hy4", "connection_tested": False})
            save_key.assert_called_once_with("https://tokenhub.tencentmaas.com/v1", "secret-for-mocked-vault")
            self.assertNotIn("secret-for-mocked-vault", (Path(directory) / "settings.json").read_text())

    def test_missing_key_does_not_create_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "app_dir", return_value=Path(directory)), \
                patch.object(config, "read_key", side_effect=config.SettingsError("not found")):
            with self.assertRaises(config.SettingsError):
                config._save_configuration({"base_url": "https://tokenhub.tencentmaas.com/v1", "model": "hy4", "key": ""})
            self.assertFalse((Path(directory) / "settings.json").exists())

    def test_other_model_is_rejected_before_storing_a_key(self):
        with patch.object(config, "_store_key") as save_key:
            with self.assertRaises(config.SettingsError):
                config._save_configuration({"base_url": "https://example.com/v1", "model": "another-model", "key": "secret"})
            save_key.assert_not_called()

    def test_initial_dialog_uses_hy4_defaults_without_writing_configuration(self):
        with patch.object(config, "settings", return_value=None), patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
            dialog.assert_called_once_with({"provider": "hunyuan", "base_url": "https://tokenhub.tencentmaas.com/v1", "model": "hy4"})


if __name__ == "__main__":
    unittest.main()
