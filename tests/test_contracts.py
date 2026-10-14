from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kvc_sft.checkpoint import resolve_adapter, resolve_resume_checkpoint
from kvc_sft.contracts import (
    ContractError,
    action_response_prefix,
    canonical_action,
    load_schema,
    loads_strict,
    validate_action_text,
)
from kvc_sft.run_manifest import MANIFEST_NAME, _load_bundle, _load_manifest, _sampling_contract, _swift_identity


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "kvc_action_smoke_v0.schema.json"


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema(SCHEMA_PATH)
        cls.action = {
            "schema": "kvc_action_smoke_v0",
            "qp": 31,
            "alf": 1,
            "sao": 1,
            "affine": 4,
            "geo": 4,
            "mip": 1,
            "lfnst": 1,
            "sbt": 1,
            "ibc": 0,
        }

    def test_canonical_action_round_trip(self) -> None:
        text = canonical_action(self.action, self.schema)
        self.assertEqual(validate_action_text(text, self.schema), self.action)

    def test_canonical_action_reorders_raw_object(self) -> None:
        reversed_action = dict(reversed(list(self.action.items())))
        expected = canonical_action(self.action, self.schema)
        self.assertEqual(canonical_action(reversed_action, self.schema), expected)

    def test_action_response_prefix(self) -> None:
        self.assertEqual(action_response_prefix(self.schema), '{"schema":"kvc_action_smoke_v0",')

    def test_duplicate_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            loads_strict('{"qp":31,"qp":32}')

    def test_unknown_action_key_is_rejected(self) -> None:
        action = dict(self.action)
        action["shell"] = "--dangerous"
        text = json.dumps(action, separators=(",", ":"))
        with self.assertRaisesRegex(ContractError, "Additional properties"):
            validate_action_text(text, self.schema)

    def test_noncanonical_whitespace_is_rejected(self) -> None:
        text = json.dumps(self.action, ensure_ascii=False)
        with self.assertRaisesRegex(ContractError, "canonical"):
            validate_action_text(text, self.schema)

    def test_inference_can_normalize_insignificant_whitespace(self) -> None:
        text = json.dumps(self.action, ensure_ascii=False)
        self.assertEqual(
            validate_action_text(text, self.schema, require_canonical=False),
            self.action,
        )

    def test_invalid_enum_is_rejected(self) -> None:
        action = dict(self.action)
        action["qp"] = 63
        text = json.dumps(action, separators=(",", ":"))
        with self.assertRaisesRegex(ContractError, "not one of"):
            validate_action_text(text, self.schema)


class CheckpointTests(unittest.TestCase):
    def test_latest_checkpoint_is_resolved_numerically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for step in (2, 10):
                checkpoint = root / f"checkpoint-{step}"
                checkpoint.mkdir()
                (checkpoint / "adapter_config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_adapter(root).name, "checkpoint-10")

    @staticmethod
    def complete_checkpoint(root: Path, *, world_size=2):
        checkpoint = root / "checkpoint-2"
        checkpoint.mkdir()
        (checkpoint / "adapter_config.json").write_text("{}", encoding="utf-8")
        (checkpoint / "trainer_state.json").write_text('{"global_step":2}', encoding="utf-8")
        filenames = ["adapter_model.safetensors", "optimizer.pt", "scheduler.pt"]
        filenames += ["rng_state.pth"] if world_size == 1 else [f"rng_state_{rank}.pth" for rank in range(world_size)]
        for filename in filenames:
            (checkpoint / filename).write_bytes(b"fixture only, not a loadable checkpoint")
        return checkpoint

    def test_complete_two_rank_checkpoint_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = self.complete_checkpoint(root)
            self.assertEqual(resolve_resume_checkpoint(root, world_size=2), checkpoint)

    def test_adapter_only_checkpoint_cannot_claim_full_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "adapter_config.json").write_text("{}", encoding="utf-8")
            (root / "adapter_model.safetensors").write_bytes(b"fixture")
            with self.assertRaisesRegex(ContractError, "cannot resume complete DDP state"):
                resolve_resume_checkpoint(root)

    def test_resume_requires_every_rank_rng_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = self.complete_checkpoint(Path(temporary), world_size=1)
            with self.assertRaisesRegex(ContractError, "rng_state_0"):
                resolve_resume_checkpoint(checkpoint, world_size=2)

    def test_resume_rejects_changed_world_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = self.complete_checkpoint(Path(temporary), world_size=3)
            with self.assertRaisesRegex(ContractError, "world_size differs"):
                resolve_resume_checkpoint(checkpoint, world_size=2)

    def test_resume_requires_completed_optimizer_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = self.complete_checkpoint(Path(temporary))
            (checkpoint / "trainer_state.json").write_text('{"global_step":0}', encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "positive completed global_step"):
                resolve_resume_checkpoint(checkpoint)

    def test_single_gpu_checkpoint_can_resume_on_same_world_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = self.complete_checkpoint(Path(temporary), world_size=1)
            self.assertEqual(resolve_resume_checkpoint(checkpoint, world_size=1), checkpoint)

    def test_resume_rejects_nonpositive_world_size(self):
        with self.assertRaisesRegex(ContractError, "world_size must be positive"):
            resolve_resume_checkpoint("unused", world_size=0)


class ManifestTests(unittest.TestCase):
    def test_base_load_bundle_pins_config_and_weights_but_not_readme(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text('{"model_type":"qwen3_5"}', encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"weights")
            (root / "README.md").write_text("first", encoding="utf-8")
            initial = _load_bundle(root, adapter=False)
            (root / "README.md").write_text("second", encoding="utf-8")
            self.assertEqual(_load_bundle(root, adapter=False), initial)
            (root / "config.json").write_text('{"model_type":"changed"}', encoding="utf-8")
            self.assertNotEqual(_load_bundle(root, adapter=False), initial)

    def test_identical_model_can_move_to_another_server_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "original"
            first.mkdir()
            (first / "config.json").write_text('{"model_type":"qwen3_5"}', encoding="utf-8")
            (first / "model.safetensors").write_bytes(b"fixture")
            second = root / "migrated"
            shutil.copytree(first, second)
            self.assertEqual(_load_bundle(first, adapter=False), _load_bundle(second, adapter=False))

    def test_download_proofs_and_license_files_do_not_change_load_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloaded = root / "verified_download"
            downloaded.mkdir()
            (downloaded / "config.json").write_text('{"model_type":"qwen3_5"}', encoding="utf-8")
            (downloaded / "model.safetensors").write_bytes(b"identical-weights")
            snapshot = root / "huggingface_snapshot"
            shutil.copytree(downloaded, snapshot)
            initial = _load_bundle(downloaded, adapter=False)
            for filename in (
                "download_manifest.json", "metadata_manifest.json", "LICENSE", "LICENSE.txt", "LICENSE.md",
                "README.md", "README", ".gitattributes",
            ):
                with self.subTest(filename=filename):
                    documentation = downloaded / filename
                    documentation.write_text("first documentation/provenance content\n", encoding="utf-8")
                    self.assertEqual(_load_bundle(downloaded, adapter=False), initial)
                    documentation.write_text("changed documentation/provenance content\n", encoding="utf-8")
                    self.assertEqual(_load_bundle(downloaded, adapter=False), initial)
                    self.assertFalse((snapshot / filename).exists())
                    self.assertEqual(_load_bundle(downloaded, adapter=False), _load_bundle(snapshot, adapter=False))

    def test_loading_configs_tokenizers_templates_code_and_weights_remain_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text('{"model_type":"qwen3_5"}', encoding="utf-8")
            (root / "model.safetensors").write_bytes(b"original-weights")
            for filename in (
                "config.json", "generation_config.json", "preprocessor_config.json", "video_preprocessor_config.json",
                "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "chat_template.jinja",
                "modeling_custom.py", "model.safetensors",
            ):
                with self.subTest(filename=filename):
                    target = root / filename
                    if not target.exists():
                        target.write_bytes(b"original-loader-content")
                    before = _load_bundle(root, adapter=False)
                    self.assertIn(filename, before["files"])
                    target.write_bytes(target.read_bytes() + b"\nchanged-loader-content")
                    self.assertNotEqual(_load_bundle(root, adapter=False), before)

    def test_legacy_qwen3vl_manifest_is_rejected_before_loading_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "checkpoint-2"
            checkpoint.mkdir()
            (checkpoint / "adapter_config.json").write_text("{}", encoding="utf-8")
            (root / MANIFEST_NAME).write_text('{"manifest_version":3}', encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "unsupported run manifest"):
                _load_manifest(root)

    def test_framework_identity_hashes_installed_source_not_unrelated_git_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "swift"
            package.mkdir()
            initializer = package / "__init__.py"
            initializer.write_text("VERSION = 'fixture'\n", encoding="utf-8")
            with mock.patch("kvc_sft.run_manifest.importlib.util.find_spec", return_value=SimpleNamespace(origin=str(initializer))), \
                    mock.patch("kvc_sft.run_manifest.importlib.metadata.version", return_value="test-version"):
                first = _swift_identity()
                initializer.write_text("VERSION = 'changed'\n", encoding="utf-8")
                second = _swift_identity()
            self.assertEqual(first["source_path"], str(package))
            self.assertEqual(first["version"], "test-version")
            self.assertNotEqual(first["source_sha256"], second["source_sha256"])

    def test_sampling_contract_rejects_inverted_limits(self) -> None:
        with self.assertRaisesRegex(ContractError, "cannot exceed"):
            _sampling_contract(
                video_backend="torchcodec",
                fps_max_frames=8,
                video_min_token_num=128,
                video_max_token_num=64,
                max_ratio=200,
                frame_factor=2,
                fps=2.0,
                fps_min_frames=4,
                spatial_merge_size=2,
                model_seq_len=128000,
            )


if __name__ == "__main__":
    unittest.main()
