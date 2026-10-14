"""Capacity fixtures must never masquerade as measured labels or new videos."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kvc_sft.contracts import ContractError
from kvc_sft.validate_dataset import VideoInfo, _tag_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kvc_capacity_fixture", PROJECT_ROOT / "scripts/make_capacity_fixture.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class CapacityFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.delivery = Path(self.temporary.name)
        (self.delivery / "source").mkdir()
        (self.delivery / "source/source.mp4").write_bytes(b"not a video; ffprobe is mocked")
        (self.delivery / "cae_predict/crf-34").mkdir(parents=True)
        self.feature_path = self.delivery / "cae_predict/crf-34/cae-features.json"
        self.features = {f"feature_{index:03d}": index + 0.123456789 for index in range(253)}
        self.feature_path.write_text(json.dumps(self.features), encoding="utf-8")
        self.video_info = VideoInfo(width=1920, height=1080, fps=30.0, frame_count=120, bit_depth=10, hdr=0)

    def rows(self, **kwargs):
        with mock.patch.object(fixture, "_probe_video", return_value=self.video_info):
            return fixture.make_capacity_rows(
                delivery=self.delivery,
                smoke_dataset=PROJECT_ROOT / "data/kvc_sft_smoke.jsonl",
                action_schema=PROJECT_ROOT / "schemas/kvc_action_smoke_v0.schema.json",
                **kwargs,
            )

    def test_default_eight_replicas_are_one_source_not_eight_independent_videos(self):
        rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({row["sample_id"] for row in rows}), 8)
        self.assertEqual(len({row["source_group"] for row in rows}), 1)
        self.assertEqual(len({row["videos"][0] for row in rows}), 1)
        self.assertTrue(all(Path(row["videos"][0]).is_absolute() for row in rows))

    def test_every_row_and_prompt_warn_that_labels_and_metrics_are_synthetic(self):
        for row in self.rows(copies=2):
            self.assertIs(row["fixture_only"], True)
            self.assertEqual(row["label_origin"], "synthetic_smoke")
            self.assertEqual(row["anchor_metrics_origin"], "synthetic_smoke")
            self.assertIn("NOT measured ground truth", row["fixture_warning"])
            self.assertTrue(row["messages"][0]["content"].startswith(fixture.WARNING))

    def test_features_are_not_clipped_rounded_or_renamed(self):
        original_bytes = self.feature_path.read_bytes()
        row = self.rows(copies=1)[0]
        actual = _tag_json(row["messages"][1]["content"], "pass2_features")
        self.assertEqual(actual, self.features)
        self.assertEqual(list(actual), list(self.features))
        self.assertGreater(max(actual.values()), 8.0)
        self.assertNotEqual(actual["feature_000"], round(actual["feature_000"], 6))
        self.assertEqual(self.feature_path.read_bytes(), original_bytes)
        self.assertIn("feature_schema=external_delivery_capacity_v0", row["messages"][1]["content"])

    def test_video_metadata_is_from_probe_not_the_smoke_video(self):
        row = self.rows(copies=1)[0]
        self.assertEqual(
            _tag_json(row["messages"][1]["content"], "video_meta"),
            {"width": 1920, "height": 1080, "fps": 30.0, "frame_count": 120, "bit_depth": 10, "hdr": 0},
        )

    def test_missing_probe_frame_count_is_not_guessed(self):
        self.video_info = VideoInfo(width=1920, height=1080, fps=30.0, frame_count=None, bit_depth=10, hdr=0)
        with self.assertRaisesRegex(ContractError, "no guessed video metadata"):
            self.rows()

    def test_wrong_feature_count_is_rejected(self):
        self.features.pop("feature_000")
        self.feature_path.write_text(json.dumps(self.features), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "exactly 253"):
            self.rows()

    def test_nonpositive_copy_count_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "copies must be positive"):
            self.rows(copies=0)

    def test_cli_never_overwrites_existing_output(self):
        output = self.delivery / "existing.jsonl"
        output.write_text("user-owned content\n", encoding="utf-8")
        with mock.patch("sys.argv", ["make_capacity_fixture.py", "--delivery", str(self.delivery), "--output", str(output)]), \
                mock.patch.object(fixture, "make_capacity_rows") as generate, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fixture.main(), 1)
        generate.assert_not_called()
        self.assertEqual(output.read_text(encoding="utf-8"), "user-owned content\n")

    def test_cli_requires_delivery_instead_of_assuming_a_sibling_directory(self):
        with mock.patch("sys.argv", ["make_capacity_fixture.py"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            fixture.main()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
