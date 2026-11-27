"""New batches choose server processing; legacy checkpoints keep their workflow."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_localizer import batch, config, seedance_batch


class ServerRoutingTests(unittest.TestCase):
    def create(self, model, backend):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic fixture; no API")
            connection = ({"provider": "seedance", "base_url": config.SEEDANCE_DEFAULT_ENDPOINT,
                           "model": model}, "synthetic-key", {})
            with patch.object(batch, "batch_lock", return_value=nullcontext()), \
                 patch.object(batch, "_connection", return_value=connection), \
                 patch.object(seedance_batch, "prepare", return_value={}), \
                 patch("video_localizer.selfhost_media.binding", return_value=backend), \
                 patch.object(batch, "_run", side_effect=lambda _f, report, *_args: report):
                return batch.convert([str(source)], root / "out")

    def test_new_seedance20_selfhost_batch_binds_server_processing(self):
        result = self.create(config.SEEDANCE_20_MODEL,
                             {"provider": "self_hosted", "endpoint": "https://media.example.test/codex-media/v1"})
        self.assertEqual(result["processing_backend"], "server-v1")
        self.assertEqual(result["schema"], 3)

    def test_other_and_legacy_backends_do_not_silently_change(self):
        for model, backend in ((config.SEEDANCE_25_MODEL, {"provider": "self_hosted", "endpoint": "https://media.example.test/codex-media/v1"}),
                               (config.SEEDANCE_20_MODEL, {"provider": "ark_files"})):
            with self.subTest(model=model, backend=backend):
                self.assertNotIn("processing_backend", self.create(model, backend))

    def test_server_route_does_not_fallback_to_local_after_error(self):
        with patch("video_localizer.seedance_server_batch.run", side_effect=ValueError("server unavailable")) as remote, \
             patch("video_localizer.seedance_segment_batch.run") as local:
            with self.assertRaisesRegex(ValueError, "server unavailable"):
                seedance_batch.run(None, {"processing_backend": "server-v1"}, None)
            remote.assert_called_once()
            local.assert_not_called()

    def test_longer_metadata_limit_is_explicit_and_bounded(self):
        media = {"durationMs": 43400, "width": 1920, "height": 1080, "fps": 60,
                 "bytes": 21_248_707, "videoCodec": "h264", "audioCodec": "aac"}
        with patch.object(seedance_batch.engine, "probe", return_value=media), \
             patch.object(seedance_batch.engine, "digest", return_value="a" * 64):
            with self.assertRaisesRegex(ValueError, "4–30"):
                seedance_batch.source_metadata(None, {})
            self.assertEqual(seedance_batch.source_metadata(None, {}, max_duration=300)["durationMs"], 43400)
            for invalid in (True, 301, 3, "300"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    seedance_batch.source_metadata(None, {}, max_duration=invalid)
            media["durationMs"] = 300001
            with self.assertRaisesRegex(ValueError, "4–300"):
                seedance_batch.source_metadata(None, {}, max_duration=300)


if __name__ == "__main__":
    unittest.main()
