import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


BUILD_PATH = Path(__file__).resolve().parents[1] / "packaging" / "build_windows.py"
SPEC = importlib.util.spec_from_file_location("video_localizer_windows_build", BUILD_PATH)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class LauncherIdentityTests(unittest.TestCase):
    def test_lf_and_crlf_checkouts_share_windows_identity_and_legacy_lf_alias(self):
        lf = b'using System;\nclass Launcher {\n    static void Main() {}\n}\n'
        crlf = lf.replace(b"\n", b"\r\n")
        expected = (hashlib.sha256(crlf).hexdigest()[:32], hashlib.sha256(lf).hexdigest()[:32])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Launcher.cs"
            for raw in (lf, crlf):
                with self.subTest(line_endings="CRLF" if b"\r\n" in raw else "LF"):
                    path.write_bytes(raw)
                    self.assertEqual(build.launcher_identities(path), expected)
                    self.assertEqual(path.read_bytes(), raw)

    def test_real_source_change_changes_both_launcher_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Launcher.cs"
            path.write_bytes(b'class Launcher { const int Version = 1; }\r\n')
            original = build.launcher_identities(path)
            path.write_bytes(b'class Launcher { const int Version = 2; }\n')
            changed = build.launcher_identities(path)
        self.assertTrue(set(original).isdisjoint(changed))

    def test_installer_skips_executable_write_for_both_equivalent_identities(self):
        script = (BUILD_PATH.parent / "installer.nsi").read_text(encoding="utf-8")
        guard = script.split('FileRead $1 $2', 1)[1].split('  write_launcher:', 1)[0]
        self.assertIn('StrCmp $2 "${LAUNCHER_ID}" launcher_present', guard)
        self.assertIn('StrCmp $2 "${LAUNCHER_LEGACY_LF_ID}" launcher_present', guard)
        write = script.split('  write_launcher:', 1)[1].split('  launcher_present:', 1)[0]
        self.assertIn('File "${LAUNCHER}"', write)
        self.assertIn('FileWrite $1 "${LAUNCHER_ID}"', write)


class OfflineCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        self.payload = b"pinned dependency"
        self.spec = {
            "file": "dependency.zip", "url": "https://example.invalid/dependency.zip",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def test_verified_cache_is_used_without_network(self):
        target = self.cache / self.spec["file"]
        target.write_bytes(self.payload)
        with patch.object(build.urllib.request, "urlopen") as network:
            self.assertEqual(build.fetch(self.cache, self.spec, offline=True), target)
        network.assert_not_called()

    def test_missing_dependency_fails_without_network(self):
        with patch.object(build.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "Offline dependency cache missing"):
                build.fetch(self.cache, self.spec, offline=True)
        network.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_tampered_dependency_is_preserved_and_rejected_without_network(self):
        target = self.cache / self.spec["file"]
        target.write_bytes(b"changed bytes")
        with patch.object(build.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                build.fetch(self.cache, self.spec, offline=True)
        network.assert_not_called()
        self.assertEqual(target.read_bytes(), b"changed bytes")


if __name__ == "__main__":
    unittest.main()
