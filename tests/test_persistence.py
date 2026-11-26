import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from video_localizer import config, executor, persistence


def windows_error(code):
    error = PermissionError(errno.EACCES, "checkpoint is temporarily open")
    error.winerror = code
    return error


class PersistenceTests(unittest.TestCase):
    def test_transient_windows_locks_retry_then_atomically_replace(self):
        real_replace = persistence.os.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder) / "new.tmp", Path(folder) / "state.json"
                source.write_bytes(b"new checkpoint")
                target.write_bytes(b"old checkpoint")
                failures = [windows_error(code), windows_error(code)]

                def replace(src, dst):
                    self.assertEqual(target.read_bytes(), b"old checkpoint")
                    if failures:
                        raise failures.pop()
                    return real_replace(src, dst)

                with patch.object(persistence.os, "replace", side_effect=replace) as mocked, \
                        patch.object(persistence.time, "sleep") as sleep:
                    persistence.atomic_replace(source, target)
                self.assertEqual(mocked.call_count, 3)
                self.assertEqual(sleep.call_args_list, [call(0.05), call(0.05)])
                self.assertEqual(target.read_bytes(), b"new checkpoint")
                self.assertFalse(source.exists())

    def test_persistent_lock_is_bounded_and_does_not_delete_target(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "new.tmp", Path(folder) / "state.json"
            source.write_bytes(b"new checkpoint")
            target.write_bytes(b"old checkpoint")
            error = windows_error(32)
            with patch.object(persistence.os, "replace", side_effect=error) as mocked, \
                    patch.object(persistence.time, "sleep") as sleep:
                with self.assertRaises(OSError) as caught:
                    persistence.atomic_replace(source, target)
            self.assertIs(caught.exception, error)
            self.assertEqual(mocked.call_count, 10)
            self.assertEqual(sleep.call_args_list, [call(0.05)] * 9)
            self.assertEqual(target.read_bytes(), b"old checkpoint")
            self.assertEqual(source.read_bytes(), b"new checkpoint")

    def test_other_filesystem_errors_are_not_retried(self):
        for error in (OSError(errno.ENOSPC, "disk full"),
                      PermissionError(errno.EACCES, "not a Windows lock"), windows_error(2)):
            with self.subTest(error=error), \
                    patch.object(persistence.os, "replace", side_effect=error) as mocked, \
                    patch.object(persistence.time, "sleep") as sleep:
                with self.assertRaises(OSError) as caught:
                    persistence.atomic_replace("new.tmp", "state.json")
                self.assertIs(caught.exception, error)
                mocked.assert_called_once_with("new.tmp", "state.json")
                sleep.assert_not_called()

    def test_json_writers_preserve_target_and_clean_temporary_on_replace_failure(self):
        for writer in (config.write_json, executor._save):
            with self.subTest(writer=writer.__module__), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "state.json"
                target.write_bytes(b'{"old": true}')
                with patch.object(persistence.os, "replace", side_effect=windows_error(5)), \
                        patch.object(persistence.time, "sleep"):
                    with self.assertRaises(OSError):
                        writer(target, {"new": True})
                self.assertEqual(target.read_bytes(), b'{"old": true}')
                self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_json_writers_recover_and_remove_temporary_files(self):
        real_replace = persistence.os.replace
        for writer in (config.write_json, executor._save):
            with self.subTest(writer=writer.__module__), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "state.json"
                target.write_text('{"old": true}', encoding="utf-8")
                attempts = []

                def replace(src, dst):
                    attempts.append((src, dst))
                    if len(attempts) == 1:
                        raise windows_error(5)
                    return real_replace(src, dst)

                with patch.object(persistence.os, "replace", side_effect=replace), \
                        patch.object(persistence.time, "sleep"):
                    writer(target, {"new": "新检查点"})
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": "新检查点"})
                self.assertEqual(len(attempts), 2)
                self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_serialization_failure_preserves_target_and_cleans_temporary(self):
        for writer in (config.write_json, executor._save):
            with self.subTest(writer=writer.__module__), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "state.json"
                target.write_bytes(b'{"old": true}')
                with self.assertRaises(TypeError):
                    writer(target, {"invalid": object()})
                self.assertEqual(target.read_bytes(), b'{"old": true}')
                self.assertEqual(list(Path(folder).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
