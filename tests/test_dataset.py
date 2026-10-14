from __future__ import annotations

import unittest
from pathlib import Path

from kvc_sft.contracts import load_schema
from kvc_sft.validate_dataset import _jsonl_rows, _validate_row


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SmokeDatasetTests(unittest.TestCase):
    def test_bundled_rows_satisfy_contract(self) -> None:
        dataset = PROJECT_ROOT / "data" / "kvc_sft_smoke.jsonl"
        schema = load_schema(PROJECT_ROOT / "schemas" / "kvc_action_smoke_v0.schema.json")
        rows = list(_jsonl_rows(dataset))
        self.assertEqual(len(rows), 2)
        for line_number, row in rows:
            feature_paths, feature_schema, media_size = _validate_row(
                line_number,
                row,
                dataset_path=dataset,
                action_schema=schema,
                expected_features=16,
                max_abs_feature=8.0,
                max_feature_decimals=6,
                probe_video=False,
            )
            self.assertEqual(len(feature_paths), 16)
            self.assertEqual(feature_schema, "pass2_smoke_v0")
            self.assertGreater(media_size, 0)


if __name__ == "__main__":
    unittest.main()
