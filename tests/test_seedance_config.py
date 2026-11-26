import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_localizer import config


class SeedanceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(config, "app_dir", return_value=self.root).start()
        self.save_key = patch.object(config, "_store_key").start()
        self.read_key = patch.object(config, "read_key", return_value="existing-synthetic-key").start()
        self.hunyuan = {"base_url": config.DEFAULT_ENDPOINT, "model": "hy4-flash"}
        self.seedance = {"provider": "seedance", "base_url": config.SEEDANCE_DEFAULT_ENDPOINT,
                         "model": config.SEEDANCE_DEFAULT_MODEL}
        self.deployment = dict(self.seedance, model="ep-20261126155948-example",
                               model_family=config.SEEDANCE_20_FAMILY)

    def write_legacy(self, **extra):
        config.write_json(self.root / "settings.json", dict(self.hunyuan, **extra))

    def save_seedance(self, **changes):
        return config._save_configuration(dict(self.seedance, key="seedance-synthetic-secret", **changes))

    def test_old_configuration_and_vault_target_remain_compatible(self):
        self.write_legacy()
        self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))
        expected = "YoungTiger.VideoLocalizer/" + hashlib.sha256(config.DEFAULT_ENDPOINT.encode()).hexdigest()
        self.assertEqual(config._target(config.DEFAULT_ENDPOINT), expected)
        config._save_configuration(dict(self.hunyuan, key=""))
        self.read_key.assert_called_once_with(config.DEFAULT_ENDPOINT)
        self.save_key.assert_not_called()

    def test_seedance_model_infers_provider_and_uses_its_own_endpoint(self):
        result = config._save_configuration({"base_url": config.SEEDANCE_DEFAULT_ENDPOINT + "/",
                                             "model": " " + config.SEEDANCE_DEFAULT_MODEL + " ",
                                             "key": "seedance-synthetic-secret"})
        self.assertEqual(config.settings(), self.seedance)
        self.assertEqual(result, {"configured": True, "provider": "seedance",
                                  "model": config.SEEDANCE_DEFAULT_MODEL, "connection_tested": False})
        self.save_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT, "seedance-synthetic-secret")

    def test_switch_preserves_allowlisted_old_settings_before_storing_key(self):
        self.write_legacy(key="legacy-synthetic-secret", extra={"token": "nested-synthetic-secret"})

        def assert_backup_exists(endpoint, key):
            profiles = json.loads((self.root / "profiles.json").read_text(encoding="utf-8"))
            self.assertEqual(profiles["hunyuan"], dict(self.hunyuan, provider="hunyuan"))
            self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))

        self.save_key.side_effect = assert_backup_exists
        self.save_seedance()
        self.assertEqual(config.settings(), self.seedance)
        for path in self.root.glob("*.json"):
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("synthetic-secret", saved)
            self.assertNotIn('"key"', saved)
            self.assertNotIn('"extra"', saved)

    def test_switch_back_restores_previous_custom_hunyuan_configuration_and_key(self):
        self.hunyuan = {"base_url": "https://example.test/hunyuan/v1", "model": "hy4-flash"}
        self.write_legacy()
        self.save_seedance()
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, provider="hunyuan")
            dialog.assert_called_once_with(dict(self.hunyuan, provider="hunyuan"))
        self.save_key.reset_mock()
        config._save_configuration(dict(self.hunyuan, provider="hunyuan", key=""))
        self.read_key.assert_called_once_with(self.hunyuan["base_url"])
        self.save_key.assert_not_called()
        self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))
        profiles = json.loads((self.root / "profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles["seedance"], self.seedance)

    def test_explicit_provider_or_model_selects_seedance_defaults(self):
        self.write_legacy()
        for selection in ({"provider": "seedance"}, {"model": config.SEEDANCE_DEFAULT_MODEL}):
            with self.subTest(selection=selection), patch.object(config, "_dialog", return_value={}) as dialog:
                config.configure(gui=True, **selection)
                dialog.assert_called_once_with(self.seedance)
        self.save_key.assert_not_called()
        self.assertFalse((self.root / "profiles.json").exists())

    def test_active_seedance_remains_default_without_explicit_selection(self):
        self.save_seedance()
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
            dialog.assert_called_once_with(self.seedance)

    def test_new_seedance_default_is_20_and_allowlist_retains_25(self):
        self.assertEqual(config.SEEDANCE_DEFAULT_MODEL, config.SEEDANCE_20_MODEL)
        self.assertEqual(config.PROVIDERS["seedance"]["models"],
                         frozenset({config.SEEDANCE_20_MODEL, config.SEEDANCE_25_MODEL}))
        script = (Path(config.__file__).parent / "configuration-dialog.ps1").read_text(encoding="utf-8")
        self.assertIn("model='" + config.SEEDANCE_20_MODEL + "'", script)

    def test_saved_25_remains_valid_and_is_not_implicitly_downgraded(self):
        old = dict(self.seedance, model=config.SEEDANCE_25_MODEL)
        config.write_json(self.root / "settings.json", old)
        config.write_json(self.root / "profiles.json", {"seedance": old})
        self.assertEqual(config.settings(), old)
        self.assertEqual(config._profiles()["seedance"], old)
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True)
            dialog.assert_called_once_with(old)
        self.save_key.assert_not_called()
        self.read_key.assert_not_called()

    def test_explicit_20_selection_reuses_existing_ark_key(self):
        old = dict(self.seedance, model=config.SEEDANCE_25_MODEL)
        config.write_json(self.root / "settings.json", old)
        config._save_configuration(dict(self.seedance, key=""))
        self.assertEqual(config.settings()["model"], config.SEEDANCE_20_MODEL)
        self.read_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT)
        self.save_key.assert_not_called()
        for path in self.root.glob("*.json"):
            self.assertNotIn("existing-synthetic-key", path.read_text(encoding="utf-8"))

    def test_explicit_25_model_still_infers_seedance_provider(self):
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, model=config.SEEDANCE_25_MODEL)
            dialog.assert_called_once_with(dict(self.seedance, model=config.SEEDANCE_25_MODEL))

    def test_provider_model_and_official_endpoint_mismatches_are_rejected(self):
        invalid = [
            dict(self.seedance, provider="hunyuan"),
            dict(self.seedance, model="hy4"),
            dict(self.seedance, model="doubao-seedance-unknown"),
            dict(self.seedance, model="doubao-seedance-2-0-fast-260128"),
            dict(self.seedance, provider="unknown"),
            dict(self.seedance, provider=[]),
            dict(self.seedance, base_url=config.DEFAULT_ENDPOINT),
            dict(self.hunyuan, provider="hunyuan", base_url=config.SEEDANCE_DEFAULT_ENDPOINT),
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(config.SettingsError):
                config._save_configuration(dict(data, key="synthetic-secret"))
        self.save_key.assert_not_called()
        self.read_key.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_provider_cannot_replace_other_provider_key_at_custom_endpoint(self):
        self.hunyuan["base_url"] = "https://example.test/v1"
        self.write_legacy()
        with self.assertRaises(config.SettingsError):
            self.save_seedance(base_url="https://example.test/v1/")
        self.save_key.assert_not_called()
        self.read_key.assert_not_called()
        self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))

    def test_existing_seedance_key_can_be_reused_without_plaintext_persistence(self):
        config._save_configuration(dict(self.seedance, key=""))
        self.read_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT)
        self.save_key.assert_not_called()
        for path in self.root.glob("*.json"):
            self.assertNotIn("existing-synthetic-key", path.read_text(encoding="utf-8"))

    def test_corrupt_profiles_abort_without_replacing_active_settings_or_key(self):
        self.write_legacy()
        (self.root / "profiles.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(config.SettingsError):
            self.save_seedance()
        self.save_key.assert_not_called()
        self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))

    def test_vault_failure_keeps_active_configuration_and_backup(self):
        self.write_legacy()
        self.save_key.side_effect = config.SettingsError("synthetic vault failure")
        with self.assertRaises(config.SettingsError):
            self.save_seedance()
        self.assertEqual(config.settings(), dict(self.hunyuan, provider="hunyuan"))
        profiles = json.loads((self.root / "profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles["hunyuan"], dict(self.hunyuan, provider="hunyuan"))

    def test_saved_provider_model_mismatch_is_rejected(self):
        config.write_json(self.root / "settings.json", dict(self.seedance, provider="hunyuan"))
        with self.assertRaises(config.SettingsError):
            config.settings()

    def test_family_resolver_keeps_deployment_id_separate_from_capabilities(self):
        self.assertEqual(config.seedance_family(config.SEEDANCE_20_MODEL), config.SEEDANCE_20_FAMILY)
        self.assertEqual(config.seedance_family(config.SEEDANCE_25_MODEL), config.SEEDANCE_25_FAMILY)
        for family in (config.SEEDANCE_20_FAMILY, config.SEEDANCE_25_FAMILY):
            self.assertEqual(config.seedance_family(self.deployment["model"], family), family)
        self.assertEqual(config.seedance_family(" " + config.SEEDANCE_20_MODEL + " ",
                                                " " + config.SEEDANCE_20_FAMILY + " "),
                         config.SEEDANCE_20_FAMILY)

    def test_endpoint_save_infers_provider_binds_family_and_reuses_existing_vault(self):
        self.write_legacy()
        supplied = {key: value for key, value in self.deployment.items() if key != "provider"}
        supplied["model"] = " " + supplied["model"] + " "
        result = config._save_configuration(dict(supplied, key=""))
        self.assertEqual(config.settings(), self.deployment)
        self.assertEqual(result["model"], self.deployment["model"])
        self.assertEqual(result["model_family"], config.SEEDANCE_20_FAMILY)
        self.read_key.assert_called_once_with(config.SEEDANCE_DEFAULT_ENDPOINT)
        self.save_key.assert_not_called()
        self.assertEqual(config._profiles()["hunyuan"], dict(self.hunyuan, provider="hunyuan"))
        self.assertEqual(config._profiles()["seedance"], self.deployment)
        for path in self.root.glob("*.json"):
            self.assertNotIn("existing-synthetic-key", path.read_text(encoding="utf-8"))

    def test_endpoint_family_and_identifier_errors_are_rejected_before_any_write(self):
        invalid = [dict(self.seedance, model=self.deployment["model"]),
                   dict(self.deployment, model_family=""),
                   dict(self.deployment, model_family="seedance-3.0"),
                   dict(self.deployment, model_family=[]),
                   dict(self.deployment, provider="hunyuan"),
                   dict(self.deployment, model="ep-"),
                   dict(self.deployment, model="ep-example/other"),
                   dict(self.deployment, model="ep-example?token=synthetic-secret"),
                   dict(self.deployment, model="ep-example\nAuthorization"),
                   dict(self.deployment, model="ep-" + "a" * 125),
                   dict(self.deployment, model=config.SEEDANCE_25_MODEL),
                   dict(self.hunyuan, model_family=config.SEEDANCE_20_FAMILY)]
        for data in invalid:
            with self.subTest(model=data["model"]), self.assertRaises(config.SettingsError) as raised:
                config._save_configuration(dict(data, key="synthetic-secret"))
            self.assertNotIn("synthetic-secret", str(raised.exception))
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_deployment_profile_round_trip_restores_explicit_family(self):
        config._save_configuration(dict(self.deployment, key=""))
        config._save_configuration(dict(self.hunyuan, key=""))
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, provider="seedance")
            dialog.assert_called_once_with(self.deployment)
        self.assertEqual(config._profiles()["seedance"], self.deployment)

    def test_saved_endpoint_without_provider_is_normalized_without_mutation(self):
        supplied = {key: value for key, value in self.deployment.items() if key != "provider"}
        path = self.root / "settings.json"
        config.write_json(path, supplied)
        original = path.read_bytes()
        self.assertEqual(config.settings(), self.deployment)
        self.assertEqual(path.read_bytes(), original)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()

    def test_configure_new_deployment_needs_its_own_family_selection(self):
        config._save_configuration(dict(self.deployment, key=""))
        another = "ep-20261126160000-other"
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, model=another)
            dialog.assert_called_once_with(dict(self.seedance, model=another))
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, model=another, model_family=config.SEEDANCE_25_FAMILY)
            dialog.assert_called_once_with(dict(self.seedance, model=another,
                                                model_family=config.SEEDANCE_25_FAMILY))

    def test_changing_explicit_direct_model_does_not_inherit_previous_family(self):
        config._save_configuration(dict(self.seedance, model=config.SEEDANCE_25_MODEL,
                                         model_family=config.SEEDANCE_25_FAMILY, key=""))
        with patch.object(config, "_dialog", return_value={}) as dialog:
            config.configure(gui=True, model=config.SEEDANCE_20_MODEL)
            dialog.assert_called_once_with(self.seedance)

    def test_terminal_deployment_prompts_for_family_and_reuses_key(self):
        with patch.object(config.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value=config.SEEDANCE_25_FAMILY) as prompt, \
             patch.object(config.getpass, "getpass", return_value=""):
            result = config.configure(endpoint=config.SEEDANCE_DEFAULT_ENDPOINT,
                                      model=self.deployment["model"])
        prompt.assert_called_once()
        self.assertEqual(result["model_family"], config.SEEDANCE_25_FAMILY)
        self.assertEqual(config.settings(), dict(self.deployment, model_family=config.SEEDANCE_25_FAMILY))
        self.save_key.assert_not_called()

    def test_explicit_terminal_family_does_not_prompt_again(self):
        with patch.object(config.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", side_effect=AssertionError("Unnecessary input")), \
             patch.object(config.getpass, "getpass", return_value=""):
            config.configure(endpoint=config.SEEDANCE_DEFAULT_ENDPOINT, model=self.deployment["model"],
                             model_family=config.SEEDANCE_20_FAMILY)
        self.assertEqual(config.settings(), self.deployment)

    def test_gui_family_round_trip_uses_only_nonsecret_environment(self):
        window = Mock()
        window.stdout = io.StringIO(json.dumps(dict(self.deployment, key="")) + "\n")
        window.stdin = io.StringIO()
        window.wait.return_value = 0
        process = Mock()
        process.__enter__ = Mock(return_value=window)
        process.__exit__ = Mock(return_value=False)
        with patch.object(config.subprocess, "Popen", return_value=process) as launch:
            result = config._dialog(self.deployment)
        self.assertEqual(launch.call_args.kwargs["env"]["VL_DIALOG_FAMILY"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(launch.call_args.kwargs["env"]["VL_DIALOG_MODEL"], self.deployment["model"])
        self.assertNotIn("existing-synthetic-key", str(launch.call_args))
        self.assertTrue(json.loads(window.stdin.getvalue())["ok"])
        self.assertEqual(result["model_family"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(config.settings(), self.deployment)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell event logic")
    def test_gui_new_endpoint_clears_family_but_exact_draft_restore_retains_it(self):
        # Parse only the real functions and event callbacks. Plain property bags
        # stand in for controls; no Forms code, window or credential is executed.
        program = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null
$errors = $null
$source = [IO.File]::ReadAllText($env:VL_TEST_DIALOG_SCRIPT, [Text.Encoding]::UTF8)
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Dialog syntax error' }
$names = @('Set-FamilyForModel','Update-FamilyForModelChange','Restore-ProviderDraft')
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names}, $true)
if ($functions.Count -ne 3) { throw 'Missing transition functions' }
foreach ($fn in $functions) { . ([scriptblock]::Create($fn.Extent.Text)) }
$modelHook = $ast.Find({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Expression.Extent.Text -eq '$model' -and $node.Member.Value -eq 'Add_TextChanged'}, $true)
$providerHook = $ast.Find({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Expression.Extent.Text -eq '$provider' -and $node.Member.Value -eq 'Add_SelectedIndexChanged'}, $true)
$onModelChange = $modelHook.Arguments[0].ScriptBlock.GetScriptBlock()
$onProviderChange = $providerHook.Arguments[0].ScriptBlock.GetScriptBlock()
function Update-ProviderText {}
$provider = [pscustomobject]@{SelectedItem='seedance'}
$model = [pscustomobject]@{Text='doubao-seedance-2-5-260628'}
$family = [pscustomobject]@{SelectedIndex=1; SelectedItem='seedance-2.5'}
$endpoint = [pscustomobject]@{Text='https://example.invalid/api/v3'}
$message = [pscustomobject]@{Text=''}
$key = [pscustomobject]@{}
$key | Add-Member ScriptMethod Clear {}
$script:currentProvider = 'seedance'
$script:currentModelId = $model.Text
$script:updatingFamily = $false
$script:restoringDraft = $false
$drafts = @{hunyuan=@{base_url='https://other.invalid/v1';model='hy4';model_family=''}}
$outcome = [ordered]@{}
$model.Text = 'ep-20261126160000-first'
& $onModelChange
$outcome.from_direct = $family.SelectedItem
$outcome.reselection_prompt = [bool]$message.Text
$family.SelectedItem = 'seedance-2.5'
$model.Text = 'ep-20261126160000-second'
& $onModelChange
$outcome.from_endpoint = $family.SelectedItem
$family.SelectedItem = 'seedance-2.0'
$model.Text = ' ep-20261126160000-second '
& $onModelChange
$outcome.same_normalized_id = $family.SelectedItem
$model.Text = 'ep-20261126160000-SECOND'
& $onModelChange
$outcome.case_changed_id = $family.SelectedItem
$model.Text = 'ep-20261126160000-second'
& $onModelChange
$family.SelectedItem = 'seedance-2.0'
$provider.SelectedItem = 'hunyuan'
& $onProviderChange
$outcome.hunyuan_family = $family.SelectedItem
$provider.SelectedItem = 'seedance'
& $onProviderChange
$outcome.restored_model = $model.Text.Trim()
$outcome.restored_family = $family.SelectedItem
$outcome.restore_guard_cleared = -not $script:restoringDraft
$model.Text = 'ep-20261126160000-third'
& $onModelChange
$outcome.changed_after_restore = $family.SelectedItem
$model.Text = 'doubao-seedance-2-0-260128'
& $onModelChange
$outcome.known_20 = $family.SelectedItem
$model.Text = 'doubao-seedance-2-5-260628'
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
        self.assertIsNone(result["hunyuan_family"])
        self.assertEqual(result["restored_model"], "ep-20261126160000-second")
        self.assertEqual(result["restored_family"], config.SEEDANCE_20_FAMILY)
        self.assertTrue(result["restore_guard_cleared"])
        self.assertIsNone(result["changed_after_restore"])
        self.assertEqual(result["known_20"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(result["known_25"], config.SEEDANCE_25_FAMILY)
        self.read_key.assert_not_called()
        self.save_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
