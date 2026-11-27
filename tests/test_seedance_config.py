import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from video_localizer import config


class SeedanceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(config, "app_dir", return_value=self.root).start()
        self.save_key = patch.object(config, "_store_key").start()
        self.read_key = patch.object(config, "read_key", side_effect=self.key_for_endpoint).start()
        self.legacy = {"provider": "hunyuan", "base_url": "https://tokenhub.tencentmaas.com/v1",
                       "model": "hy4-flash"}
        self.seedance = {"provider": "seedance", "base_url": config.SEEDANCE_DEFAULT_ENDPOINT,
                         "model": config.SEEDANCE_DEFAULT_MODEL}
        self.deployment = dict(self.seedance, model="ep-20261126155948-example",
                               model_family=config.SEEDANCE_20_FAMILY)
        self.media = {"provider": "self_hosted", "endpoint": "https://media.example.com/codex-media/v1"}

    def key_for_endpoint(self, endpoint):
        return "media-synthetic-secret" if endpoint.endswith("/codex-media/v1") else "ark-synthetic-secret"

    def save(self, **changes):
        return config._save_configuration(dict(self.seedance, **{"key": "ark-new-secret", **changes}))

    def stored(self, filename):
        return json.loads((self.root / filename).read_text(encoding="utf-8"))

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.root.iterdir()}

    def assert_no_secrets(self):
        for path in self.root.glob("*.json"):
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", saved)
            self.assertNotIn('"key"', saved)

    def test_default_is_seedance_and_only_seedance_is_an_active_provider(self):
        self.assertEqual(set(config.PROVIDERS), {"seedance"})
        self.assertEqual(config.SEEDANCE_DEFAULT_MODEL, config.SEEDANCE_20_MODEL)
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
        dialog.assert_called_once_with(self.seedance)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_direct_model_round_trip_normalizes_and_does_not_persist_key(self):
        result = config._save_configuration({"base_url": config.SEEDANCE_DEFAULT_ENDPOINT + "/",
                                             "model": " " + config.SEEDANCE_DEFAULT_MODEL + " ",
                                             "key": "ark-new-secret"})
        self.assertEqual(config.settings(), self.seedance)
        self.assertFalse(result["connection_tested"])
        self.assertFalse(result["media_configured"])
        self.save_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT, "ark-new-secret")
        self.assert_no_secrets()

    def test_saved_25_is_preserved_and_explicit_20_reuses_ark_key(self):
        old = dict(self.seedance, model=config.SEEDANCE_25_MODEL)
        config.write_json(self.root / "settings.json", old)
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
        dialog.assert_called_once_with(old)
        self.save(key="")
        self.assertEqual(config.settings(), self.seedance)
        self.read_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT)
        self.save_key.assert_not_called()

    def test_legacy_and_invalid_model_provider_endpoint_are_rejected_before_vault(self):
        invalid = [dict(self.seedance, provider="hunyuan"), self.legacy,
                   dict(self.seedance, provider=[]), dict(self.seedance, provider="unknown"),
                   dict(self.seedance, model="doubao-seedance-unknown"),
                   dict(self.seedance, model="doubao-seedance-2-0-fast-260128"),
                   dict(self.seedance, base_url=self.legacy["base_url"])]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(config.SettingsError):
                config._save_configuration(dict(data, key="test-secret"))
        with self.assertRaises(config.SettingsError):
            config.configure(gui=True, provider="hunyuan")
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()
        self.assertEqual(self.snapshot(), {})

    def test_legacy_profiles_do_not_prevent_reading_saved_seedance_profile(self):
        config.write_json(self.root / "settings.json", self.legacy)
        # Opening a supported saved profile does not interpret an obsolete entry.
        config.write_json(self.root / "profiles.json", {"hunyuan": {"obsolete": True},
                                                        "seedance": self.deployment})
        original = self.snapshot()
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
        dialog.assert_called_once_with(self.deployment)
        self.assertEqual(config._profiles(), {"seedance": self.deployment})
        self.assertEqual(self.snapshot(), original)
        self.read_key.assert_not_called()

    def test_active_seedance_rejects_loopback_http_before_credentials_or_writes(self):
        for endpoint in ("http://localhost/v1", "http://127.0.0.1:8080/v1", "http://[::1]/v1"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(config.SettingsError):
                    self.save(base_url=endpoint)
                self.assertEqual(self.snapshot(), {})
                config.write_json(self.root / "settings.json", dict(self.seedance, base_url=endpoint))
                with self.assertRaises(config.SettingsError):
                    config.settings()
                (self.root / "settings.json").unlink()
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_legacy_loopback_profile_remains_recoverable_without_vault_access(self):
        legacy = dict(self.legacy, base_url="http://127.0.0.1:8080/v1")
        config.write_json(self.root / "settings.json", legacy)
        config.write_json(self.root / "profiles.json", {"hunyuan": legacy})
        self.assertEqual(config.migrate_legacy_profiles()["legacy_profiles_archived"], 1)
        self.assertEqual(self.stored("legacy-profiles.json"), {"hunyuan": legacy})
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_save_archives_legacy_allowlist_and_keeps_old_vault_target(self):
        config.write_json(self.root / "settings.json", dict(self.legacy, key="legacy-secret",
                                                           extra={"token": "nested-secret"}))
        config.write_json(self.root / "profiles.json", {"hunyuan": self.legacy})
        expected = "YoungTiger.VideoLocalizer/" + hashlib.sha256(self.legacy["base_url"].encode()).hexdigest()
        self.assertEqual(config._target(self.legacy["base_url"]), expected)
        self.save()
        self.assertEqual(self.stored("legacy-profiles.json"), {"hunyuan": self.legacy})
        self.assertEqual(config._profiles(), {"seedance": self.seedance})
        self.assertEqual(self.stored("profiles.json"), {"seedance": self.seedance})
        self.assertEqual(config.settings(), self.seedance)
        self.save_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT, "ark-new-secret")
        self.read_key.assert_not_called()
        self.assert_no_secrets()

    def test_migration_keeps_distinct_legacy_profiles_and_is_idempotent_without_vault(self):
        alternate = dict(self.legacy, model="hy4", base_url="https://legacy.example.com/v1")
        config.write_json(self.root / "settings.json", alternate)
        config.write_json(self.root / "profiles.json", {"hunyuan": self.legacy, "seedance": self.seedance})
        result = config.migrate_legacy_profiles()
        self.assertEqual(result, {"legacy_profiles_archived": 2, "active_providers": ["seedance"]})
        self.assertCountEqual(self.stored("legacy-profiles.json").values(), [self.legacy, alternate])
        self.assertFalse((self.root / "settings.json").exists())
        original = self.snapshot()
        self.assertEqual(config.migrate_legacy_profiles(), result)
        self.assertEqual(self.snapshot(), original)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_bad_archives_or_profiles_abort_before_configuration_and_vault_writes(self):
        for filename in ("profiles.json", "legacy-profiles.json"):
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_text("{broken", encoding="utf-8")
                original = self.snapshot()
                with self.assertRaises(config.SettingsError):
                    self.save()
                self.assertEqual(self.snapshot(), original)
                path.unlink()
        self.save_key.assert_not_called()
        self.read_key.assert_not_called()

    def test_old_vault_entry_cannot_be_replaced_even_at_custom_api_endpoint(self):
        legacy = dict(self.legacy, base_url="https://custom.example.com/v1")
        config.write_json(self.root / "settings.json", legacy)
        original = self.snapshot()
        with self.assertRaises(config.SettingsError):
            self.save(base_url=legacy["base_url"])
        self.assertEqual(self.snapshot(), original)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_vault_write_failure_leaves_previous_active_settings_and_recoverable_archive(self):
        config.write_json(self.root / "settings.json", self.legacy)
        self.save_key.side_effect = config.SettingsError("Windows vault unavailable")
        with self.assertRaises(config.SettingsError):
            self.save()
        self.assertEqual(self.stored("settings.json"), self.legacy)
        self.assertEqual(self.stored("legacy-profiles.json"), {"hunyuan": self.legacy})

    def test_family_resolver_separates_deployment_id_from_capabilities(self):
        for model, family in ((config.SEEDANCE_20_MODEL, config.SEEDANCE_20_FAMILY),
                              (config.SEEDANCE_25_MODEL, config.SEEDANCE_25_FAMILY)):
            self.assertEqual(config.seedance_family(model), family)
            self.assertEqual(config.seedance_family(self.deployment["model"], family), family)
        config._save_configuration(dict(self.deployment, key=""))
        self.assertEqual(config.settings(), self.deployment)
        self.save_key.assert_not_called()

    def test_deployment_family_and_identifier_errors_fail_without_side_effects(self):
        invalid = [dict(self.seedance, model=self.deployment["model"]),
                   dict(self.deployment, model_family=""), dict(self.deployment, model_family=[]),
                   dict(self.deployment, model_family="seedance-3.0"),
                   dict(self.deployment, model="ep-"), dict(self.deployment, model="ep-x/other"),
                   dict(self.deployment, model="ep-x?token=test-secret"),
                   dict(self.deployment, model="ep-x\nAuthorization"),
                   dict(self.deployment, model="ep-" + "a" * 125),
                   dict(self.deployment, model=config.SEEDANCE_25_MODEL)]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(config.SettingsError) as raised:
                config._save_configuration(dict(data, key="test-secret"))
            self.assertNotIn("test-secret", str(raised.exception))
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()
        self.assertEqual(self.snapshot(), {})

    def test_new_deployment_does_not_inherit_previous_family(self):
        config.write_json(self.root / "settings.json", self.deployment)
        for model in ("ep-another", self.deployment["model"].upper().replace("EP-", "ep-"),
                      config.SEEDANCE_25_MODEL):
            with self.subTest(model=model), patch.object(config, "_dialog", return_value={}) as dialog:
                config.configure(gui=True, model=model)
                dialog.assert_called_once_with(dict(self.seedance, model=model))

    def test_same_deployment_retains_family_without_writing_settings(self):
        supplied = {k: v for k, v in self.deployment.items() if k != "provider"}
        config.write_json(self.root / "settings.json", supplied)
        original = self.snapshot()
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, model=self.deployment["model"])
        dialog.assert_called_once_with(self.deployment)
        self.assertEqual(config.settings(), self.deployment)
        self.assertEqual(self.snapshot(), original)
        self.read_key.assert_not_called()

    def test_terminal_new_deployment_prompts_for_explicit_family(self):
        with patch.object(config.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value=config.SEEDANCE_25_FAMILY) as prompt, \
             patch.object(config.getpass, "getpass", return_value=""):
            result = config.configure(endpoint=config.SEEDANCE_DEFAULT_ENDPOINT, model="ep-example")
        prompt.assert_called_once()
        self.assertEqual(result["model_family"], config.SEEDANCE_25_FAMILY)
        self.save_key.assert_not_called()

    def test_media_endpoint_rejects_nonpublic_credentialed_or_model_hosts_without_vault(self):
        invalid = ["http://media.example.com/codex-media/v1", "https://localhost/codex-media/v1",
                   "https://127.0.0.1/codex-media/v1", "https://10.1.2.3/codex-media/v1",
                   "https://127.1/codex-media/v1", "https://[::1]/codex-media/v1",
                   "https://media.internal/codex-media/v1", "https://media.local/codex-media/v1",
                   "https://media.example.com:8443/codex-media/v1",
                   "https://user:secret@media.example.com/codex-media/v1",
                   self.media["endpoint"] + "?token=secret", self.media["endpoint"] + "?",
                   self.media["endpoint"] + "#", "https://media.example.com/other",
                   "https://ark.cn-beijing.volces.com/codex-media/v1",
                   "https://ark.cn-shanghai.volces.com/codex-media/v1",
                   "https://model.example.com/codex-media/v1"]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(config.SettingsError) as raised:
                config.save_media_configuration(endpoint, "upload-secret",
                                                ark_endpoint="https://model.example.com/api/v3")
            self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.snapshot(), {})
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_media_endpoint_normalization_and_vault_target_match_existing_uploader(self):
        config.save_media_configuration(self.media["endpoint"] + "/", "upload-secret")
        self.assertEqual(config.media_settings(), self.media)
        self.save_key.assert_called_once_with(self.media["endpoint"], "upload-secret")
        expected = "YoungTiger.VideoLocalizer/" + hashlib.sha256(self.media["endpoint"].encode()).hexdigest()
        self.assertEqual(config._target(self.media["endpoint"]), expected)
        self.assert_no_secrets()

    def test_combined_save_stores_independent_keys_only_at_their_own_endpoints(self):
        result = self.save(media_endpoint=self.media["endpoint"], media_key="upload-secret")
        self.assertEqual(self.save_key.call_args_list,
                         [call(config.SEEDANCE_DEFAULT_ENDPOINT, "ark-new-secret"),
                          call(self.media["endpoint"], "upload-secret")])
        self.assertEqual(config.media_settings(), self.media)
        self.assertTrue(result["media_configured"])
        self.read_key.assert_not_called()
        self.assert_no_secrets()

    def test_blank_keys_reuse_two_distinct_vault_entries(self):
        self.save(key="", media_endpoint=self.media["endpoint"], media_key="")
        self.assertEqual(self.read_key.call_args_list,
                         [call(config.SEEDANCE_DEFAULT_ENDPOINT), call(self.media["endpoint"])])
        self.save_key.assert_not_called()
        self.assert_no_secrets()

    def test_standalone_media_update_reuses_existing_upload_key_and_preserves_model(self):
        config.write_json(self.root / "settings.json", self.deployment)
        original = (self.root / "settings.json").read_bytes()
        config.save_media_configuration(self.media["endpoint"])
        self.assertEqual((self.root / "settings.json").read_bytes(), original)
        self.assertEqual(config.media_settings(), self.media)
        self.assertEqual(self.read_key.call_args_list,
                         [call(config.SEEDANCE_DEFAULT_ENDPOINT), call(self.media["endpoint"])])
        self.save_key.assert_not_called()

    def test_omitted_media_fields_preserve_exact_existing_file_and_validate_its_key(self):
        media_path = self.root / "media-upload.json"
        media_path.write_text(json.dumps(self.media, separators=(",", ":")), encoding="utf-8")
        original = media_path.read_bytes()
        result = self.save()
        self.assertEqual(media_path.read_bytes(), original)
        self.assertTrue(result["media_configured"])
        self.read_key.assert_called_once_with(self.media["endpoint"])
        self.save_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT, "ark-new-secret")

    def test_missing_upload_key_prevents_ark_key_or_file_changes(self):
        config.write_json(self.root / "settings.json", self.seedance)
        original = self.snapshot()
        self.read_key.side_effect = config.SettingsError("missing")
        with self.assertRaises(config.SettingsError):
            self.save(media_endpoint=self.media["endpoint"], media_key="")
        self.save_key.assert_not_called()
        self.assertEqual(self.snapshot(), original)

    def test_ark_key_cannot_be_copied_into_media_configuration(self):
        for media_key in ("ark-any-test-secret", "ark-new-secret"):
            with self.subTest(key=media_key), self.assertRaises(config.SettingsError):
                self.save(media_endpoint=self.media["endpoint"], media_key=media_key)
        # Opaque custom-gateway keys are compared too, including saved entries.
        self.read_key.side_effect = None
        self.read_key.return_value = "same-opaque-secret"
        with self.assertRaises(config.SettingsError):
            self.save(key="", media_endpoint=self.media["endpoint"], media_key="")
        self.assertEqual(self.snapshot(), {})
        self.save_key.assert_not_called()

    def test_standalone_media_rejects_opaque_ark_key(self):
        config.write_json(self.root / "settings.json", self.seedance)
        self.read_key.side_effect = None
        self.read_key.return_value = "opaque-ark-secret"
        original = self.snapshot()
        with self.assertRaises(config.SettingsError):
            config.save_media_configuration(self.media["endpoint"], "opaque-ark-secret")
        self.assertEqual(self.snapshot(), original)
        self.save_key.assert_not_called()

    def test_invalid_keys_and_partial_media_fields_fail_before_writes(self):
        invalid = [{"key": None}, {"key": "x\nsecret"}, {"key": "x" * 2561},
                   {"media_key": "orphan-secret"}, {"media_endpoint": "", "media_key": "upload-secret"},
                   {"media_endpoint": self.media["endpoint"], "media_key": None},
                   {"media_endpoint": self.media["endpoint"], "media_key": "x\nsecret"}]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(config.SettingsError) as raised:
                self.save(**change)
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(self.snapshot(), {})
        self.save_key.assert_not_called()

    def test_existing_media_and_new_ark_endpoint_cannot_share_host(self):
        config.write_json(self.root / "media-upload.json", self.media)
        original = self.snapshot()
        with self.assertRaises(config.SettingsError):
            self.save(base_url="https://media.example.com/api/v3")
        self.assertEqual(self.snapshot(), original)
        self.save_key.assert_not_called()
        self.read_key.assert_not_called()

    def test_gui_loads_nonsecret_media_endpoint_and_round_trips_ep_family(self):
        config.write_json(self.root / "media-upload.json", self.media)
        window = Mock()
        window.stdout = io.StringIO(json.dumps(dict(self.deployment, key="", media_endpoint=self.media["endpoint"],
                                                    media_key="")) + "\n")
        window.stdin = io.StringIO()
        window.wait.return_value = 0
        process = Mock()
        process.__enter__ = Mock(return_value=window)
        process.__exit__ = Mock(return_value=False)
        with patch.object(config.subprocess, "Popen", return_value=process) as launch:
            result = config._dialog(self.deployment)
        env = launch.call_args.kwargs["env"]
        self.assertEqual(env["VL_DIALOG_FAMILY"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(env["VL_DIALOG_MEDIA_API"], self.media["endpoint"])
        self.assertEqual(env["VL_DIALOG_MODEL"], self.deployment["model"])
        self.assertNotIn("VL_DIALOG_PROVIDER", env)
        self.assertNotIn("synthetic-secret", str(launch.call_args))
        self.assertTrue(json.loads(window.stdin.getvalue())["ok"])
        self.assertTrue(result["media_configured"])
        self.assertEqual(config.settings(), self.deployment)

    def test_gui_model_only_save_explicitly_reports_missing_upload_configuration(self):
        window = Mock(stdout=io.StringIO(json.dumps(dict(self.seedance, key="")) + "\n"), stdin=io.StringIO())
        window.wait.return_value = 0
        process = Mock(__enter__=Mock(return_value=window), __exit__=Mock(return_value=False))
        with patch.object(config.subprocess, "Popen", return_value=process):
            result = config._dialog(self.seedance)
        self.assertFalse(result["media_configured"])
        self.assertIn("尚未配置", json.loads(window.stdin.getvalue())["message"])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell transition logic")
    def test_real_gui_syntax_and_model_transition_callbacks_without_opening_ui(self):
        program = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null
$errors = $null
$source = [IO.File]::ReadAllText($env:VL_TEST_DIALOG_SCRIPT, [Text.Encoding]::UTF8)
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Dialog syntax error' }
$names = @('Set-FamilyForModel','Update-FamilyForModelChange')
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names}, $true)
if ($functions.Count -ne 2) { throw 'Missing transition functions' }
foreach ($fn in $functions) { . ([scriptblock]::Create($fn.Extent.Text)) }
$modelHook = $ast.Find({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Expression.Extent.Text -eq '$model' -and $node.Member.Value -eq 'Add_TextChanged'}, $true)
$familyHook = $ast.Find({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Expression.Extent.Text -eq '$family' -and $node.Member.Value -eq 'Add_SelectedIndexChanged'}, $true)
$onModelChange = $modelHook.Arguments[0].ScriptBlock.GetScriptBlock()
$onFamilyChange = $familyHook.Arguments[0].ScriptBlock.GetScriptBlock()
$model = [pscustomobject]@{Text='doubao-seedance-2-5-260628'}
$family = [pscustomobject]@{SelectedIndex=1; SelectedItem='seedance-2.5'}
$message = [pscustomobject]@{Text=''}
$script:currentModelId = $model.Text
$script:updatingFamily = $false
$outcome = [ordered]@{}
$model.Text = 'ep-first'
& $onModelChange
$outcome.from_direct = $family.SelectedItem
$outcome.reselection_prompt = [bool]$message.Text
$family.SelectedItem = 'seedance-2.5'
$model.Text = 'ep-second'
& $onModelChange
$outcome.from_endpoint = $family.SelectedItem
$family.SelectedItem = 'seedance-2.0'
$model.Text = ' ep-second '
& $onModelChange
$outcome.same_normalized_id = $family.SelectedItem
$model.Text = 'ep-SECOND'
& $onModelChange
$outcome.case_changed_id = $family.SelectedItem
$family.SelectedItem = 'seedance-2.5'
& $onFamilyChange
$outcome.family_does_not_replace_ep = $model.Text
$model.Text = 'doubao-seedance-2-0-260128'
& $onModelChange
$outcome.known_20 = $family.SelectedItem
$family.SelectedItem = 'seedance-2.5'
& $onFamilyChange
$outcome.family_changes_direct_model = $model.Text
& $onModelChange
$outcome.known_25 = $family.SelectedItem
$outcome | ConvertTo-Json -Compress
'''
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
        env = dict(os.environ, VL_TEST_DIALOG_SCRIPT=str(Path(config.__file__).parent / "configuration-dialog.ps1"))
        process = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                 capture_output=True, env=env, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        result = json.loads(process.stdout.decode("utf-8-sig"))
        self.assertIsNone(result["from_direct"])
        self.assertIsNone(result["from_endpoint"])
        self.assertTrue(result["reselection_prompt"])
        self.assertEqual(result["same_normalized_id"], config.SEEDANCE_20_FAMILY)
        self.assertIsNone(result["case_changed_id"])
        self.assertEqual(result["family_does_not_replace_ep"], "ep-SECOND")
        self.assertEqual(result["known_20"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(result["family_changes_direct_model"], config.SEEDANCE_25_MODEL)
        self.assertEqual(result["known_25"], config.SEEDANCE_25_FAMILY)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
