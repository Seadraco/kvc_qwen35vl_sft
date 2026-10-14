"""Full-state resume must not silently change the experiment being continued."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from kvc_sft.contracts import ContractError, TEMPLATE_CONTRACT
from kvc_sft.launch_contract import build_contract, effective_config, verify_resume_contract, verify_saved_training_args


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ResumeContractTests(unittest.TestCase):
    def fixture(self):
        return {
            "contract_version": 1,
            "immutable": {
                "training_config": {
                    "model": "Qwen/Qwen3.5-9B",
                    "model_revision": "pinned-revision",
                    "learning_rate": 0.0001,
                    **TEMPLATE_CONTRACT,
                },
                "dataset_sha256": {"dataset": ["training-hash"]},
                "action_schema_sha256": "schema-hash",
                "feature_schema": "pass2_253_v1",
                "feature_count": 253,
                "feature_limits": {"max_abs": "8", "max_decimals": "6"},
                "sampling": {"FPS_MAX_FRAMES": "16", "VIDEO_MAX_TOKEN_NUM": "128"},
                "base_model_bundle": {"sha256": "model-hash", "files": {}},
                "plugin_sha256": ["plugin-hash"],
                "swift_source_sha256": "swift-hash",
                "world_size": 2,
                "global_batch_size": 8,
            },
            "batch": {"micro_batch": 1, "gradient_accumulation_steps": 4},
            "operational": {"max_steps": 10, "save_steps": 2, "logging_steps": 1, "save_total_limit": 2},
        }

    def test_unchanged_experiment_can_resume_an_unfinished_schedule(self):
        previous = self.fixture()
        verify_resume_contract(previous, deepcopy(previous), completed_steps=4)

    def test_schedule_can_extend_but_does_not_imply_identical_uninterrupted_training(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["operational"]["max_steps"] = 12
        verify_resume_contract(previous, current, completed_steps=10)

    def test_completed_schedule_cannot_report_a_noop_as_successful_resume(self):
        previous = self.fixture()
        with self.assertRaisesRegex(ContractError, "must exceed completed"):
            verify_resume_contract(previous, deepcopy(previous), completed_steps=10)

    def test_schedule_cannot_be_shortened_even_if_steps_remain(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["operational"]["max_steps"] = 8
        with self.assertRaisesRegex(ContractError, "not shorten"):
            verify_resume_contract(previous, current, completed_steps=4)

    def test_explicit_schedule_cannot_silently_become_epoch_based(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["operational"]["max_steps"] = None
        with self.assertRaisesRegex(ContractError, "epoch schedule"):
            verify_resume_contract(previous, current, completed_steps=4)

    def test_microbatch_and_accumulation_may_change_with_same_global_batch(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["batch"] = {"micro_batch": 2, "gradient_accumulation_steps": 2}
        verify_resume_contract(previous, current, completed_steps=4)

    def test_global_batch_or_gpu_count_changes_are_rejected(self):
        previous = self.fixture()
        for key, value in (("global_batch_size", 16), ("world_size", 1)):
            with self.subTest(key=key):
                current = deepcopy(previous)
                current["immutable"][key] = value
                with self.assertRaisesRegex(ContractError, key):
                    verify_resume_contract(previous, current, completed_steps=4)

    def test_model_template_loss_and_learning_rate_changes_are_rejected(self):
        previous = self.fixture()
        for key, value in (
            ("model", "Qwen/Qwen3.5-4B"), ("model_revision", "changed-revision"),
            ("learning_rate", 0.01), ("enable_thinking", True), ("loss_scale", "default"),
            ("response_prefix", '{"schema":'),
        ):
            with self.subTest(key=key):
                current = deepcopy(previous)
                current["immutable"]["training_config"][key] = value
                with self.assertRaisesRegex(ContractError, "training_config"):
                    verify_resume_contract(previous, current, completed_steps=4)

    def test_data_schema_features_sampling_and_code_changes_are_rejected(self):
        previous = self.fixture()
        for key, value in (
            ("dataset_sha256", {"dataset": ["new-labels"]}),
            ("action_schema_sha256", "new-action-schema"),
            ("feature_schema", "changed-features"), ("feature_count", 16),
            ("feature_limits", {"max_abs": "999", "max_decimals": "6"}),
            ("sampling", {"FPS_MAX_FRAMES": "8", "VIDEO_MAX_TOKEN_NUM": "64"}),
            ("base_model_bundle", {"sha256": "changed-weights", "files": {}}),
            ("plugin_sha256", ["modified-plugin"]), ("swift_source_sha256", "modified-swift"),
        ):
            with self.subTest(key=key):
                current = deepcopy(previous)
                current["immutable"][key] = value
                with self.assertRaisesRegex(ContractError, key):
                    verify_resume_contract(previous, current, completed_steps=4)

    def test_save_and_logging_frequency_changes_are_operational(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["operational"].update(save_steps=5, logging_steps=2, save_total_limit=3)
        verify_resume_contract(previous, current, completed_steps=4)

    def test_unknown_contract_version_is_rejected(self):
        previous = self.fixture()
        current = deepcopy(previous)
        current["contract_version"] = 0
        with self.assertRaisesRegex(ContractError, "version"):
            verify_resume_contract(previous, current, completed_steps=4)


class EffectiveConfigurationTests(unittest.TestCase):
    def command(self, *options):
        return ["swift", "sft", str(PROJECT_ROOT / "configs/train_lora.yaml"), *options]

    def test_actual_yaml_meets_the_audited_template_contract(self):
        actual = effective_config(self.command())
        for key, value in TEMPLATE_CONTRACT.items():
            self.assertEqual(actual.get(key), value)
        self.assertNotIn("ENV", actual)

    def test_cli_overrides_follow_last_option_wins(self):
        actual = effective_config(self.command("--max_steps", "2", "--max_steps", "3"))
        self.assertEqual(actual["max_steps"], 3)

    def test_list_arguments_are_preserved(self):
        actual = effective_config(self.command("--external_plugins", "/first/plugin.py", "/second/plugin.py"))
        self.assertEqual(actual["external_plugins"], ["/first/plugin.py", "/second/plugin.py"])

    def test_custom_yaml_cannot_change_thinking_loss_or_prefill_after_audit(self):
        for key, value in (
            ("enable_thinking", "true"), ("loss_scale", "default"),
            ("template", "qwen3_vl"), ("add_non_thinking_prefix", "false"),
            ("response_prefix", "forced-prefix"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ContractError, "training/template audit mismatch"):
                effective_config(self.command(f"--{key}", value))

    def test_full_finetuning_or_adapter_only_save_is_not_silently_accepted(self):
        for key, value in (("tuner_type", "full"), ("model_type", "qwen3_vl"), ("save_only_model", "true")):
            with self.subTest(key=key), self.assertRaises(ContractError):
                effective_config(self.command(f"--{key}", value))

    def test_missing_cli_values_are_rejected(self):
        with self.assertRaisesRegex(ContractError, "requires a value"):
            effective_config(self.command("--max_steps"))

    def test_saved_framework_args_must_match_the_launch_contract(self):
        config = effective_config(self.command())
        previous = {"immutable": {"training_config": config}}
        saved = deepcopy(config)
        saved["target_modules"] = [saved["target_modules"]]
        verify_saved_training_args(saved, previous)

    def test_saved_model_template_and_optimizer_drift_is_rejected(self):
        config = effective_config(self.command())
        previous = {"immutable": {"training_config": config}}
        for key, value in (
            ("model_type", "qwen3_vl"), ("model_revision", "other-revision"),
            ("template", "qwen3_vl"), ("enable_thinking", True),
            ("loss_scale", "default"), ("lora_rank", 64), ("freeze_vit", False),
            ("learning_rate", 0.01), ("torch_dtype", "float32"), ("optim", "sgd"),
        ):
            with self.subTest(key=key):
                saved = deepcopy(config)
                saved[key] = value
                with self.assertRaisesRegex(ContractError, key):
                    verify_saved_training_args(saved, previous)

    def test_identical_local_model_bundle_can_resume_at_a_different_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "local_server"
            second = Path(temporary) / "h800_server"
            first.mkdir()
            second.mkdir()
            bundle = {"sha256": "same-model-hash", "files": {"model.safetensors": "same-weight-hash"}}
            common = [
                "--dataset", str(PROJECT_ROOT / "data/kvc_sft_smoke.jsonl"),
                "--external_plugins", str(PROJECT_ROOT / "src/kvc_sft/torchcodec_compat.py"),
                "--max_steps", "10",
            ]
            with mock.patch("kvc_sft.launch_contract._load_bundle", return_value=bundle), \
                    mock.patch("kvc_sft.launch_contract._swift_identity", return_value={"source_sha256": "same-swift-hash"}), \
                    mock.patch.dict(os.environ, {"NPROC_PER_NODE": "2"}):
                contracts = [
                    build_contract(
                        self.command(*common, "--model", str(path)),
                        schema=PROJECT_ROOT / "schemas/kvc_action_smoke_v0.schema.json",
                        feature_schema="pass2_smoke_v0",
                        feature_count=16,
                    )
                    for path in (first, second)
                ]
            self.assertNotEqual(contracts[0]["locations"], contracts[1]["locations"])
            self.assertEqual(contracts[0]["immutable"], contracts[1]["immutable"])
            self.assertEqual(contracts[0]["immutable"]["training_config"]["model"], "local_bundle:same-model-hash")
            verify_resume_contract(*contracts, completed_steps=2)


if __name__ == "__main__":
    unittest.main()
