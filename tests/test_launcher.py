"""Exercise shell launch contracts without touching CUDA or starting training."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def launch(self, updates=None, *, inference=False):
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("KVC_") or key in {
                "CUDA_VISIBLE_DEVICES", "NPROC_PER_NODE", "NNODES", "NODE_RANK",
                "MASTER_ADDR", "MASTER_PORT", "WORLD_SIZE", "LOCAL_WORLD_SIZE", "RANK", "LOCAL_RANK",
            }:
                environment.pop(key)
        environment.update({"KVC_SFT_PYTHON": sys.executable, "KVC_SWIFT_BIN": shutil.which("true")})
        environment.update(updates or {})
        function = "kvc_configure_inference_device" if inference else "kvc_configure_training_devices"
        script = (
            'source "$1"\n'
            f'{function}\n'
            '"$PYTHON_BIN" -c \'import json, os; print(json.dumps({k: os.environ.get(k) for k in '
            '["CUDA_VISIBLE_DEVICES", "NPROC_PER_NODE", "NNODES", "WORLD_SIZE", '
            '"KVC_PER_DEVICE_BATCH_SIZE", "KVC_GRADIENT_ACCUMULATION_STEPS"]}))\'\n'
        )
        return subprocess.run(
            ["bash", "-c", script, "launcher-test", str(PROJECT_ROOT / "scripts/common.sh")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_default_is_one_controller_with_two_workers(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(state["CUDA_VISIBLE_DEVICES"], "0,1")
        self.assertEqual(state["NPROC_PER_NODE"], "2")
        self.assertEqual(state["NNODES"], "1")
        self.assertIn("global_batch=8", result.stdout)

    def test_gpu_indices_are_preserved_and_workers_match(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "4,5"})
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(state["CUDA_VISIBLE_DEVICES"], "4,5")
        self.assertEqual(state["NPROC_PER_NODE"], "2")

    def test_single_gpu_debug_remains_available(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "5"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["NPROC_PER_NODE"], "1")

    def test_larger_microbatch_can_preserve_global_batch(self):
        result = self.launch({"KVC_PER_DEVICE_BATCH_SIZE": "2", "KVC_GRADIENT_ACCUMULATION_STEPS": "2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("global_batch=8", result.stdout)

    def test_duplicate_cards_are_rejected(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "4,4"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate", result.stderr)

    def test_invalid_visible_device_lists_are_rejected(self):
        for value in ("", "0,", ",1", "0, 1", "0,x", "-1"):
            with self.subTest(value=value):
                result = self.launch({"CUDA_VISIBLE_DEVICES": value})
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_nested_torchrun_is_rejected_even_for_rank_zero(self):
        for key in ("RANK", "LOCAL_RANK"):
            with self.subTest(key=key):
                result = self.launch({key: "0"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not inside torchrun", result.stderr)

    def test_mismatched_process_count_is_rejected(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "4,5", "NPROC_PER_NODE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must equal", result.stderr)

    def test_multi_node_launch_is_rejected(self):
        result = self.launch({"NNODES": "2"})
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_batch_is_rejected(self):
        for key in ("KVC_PER_DEVICE_BATCH_SIZE", "KVC_GRADIENT_ACCUMULATION_STEPS"):
            for value in ("0", "-1", "1.5", "two"):
                with self.subTest(key=key, value=value):
                    result = self.launch({key: value})
                    self.assertNotEqual(result.returncode, 0)

    def test_inference_clears_training_worker_environment(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "4", "NPROC_PER_NODE": "2", "WORLD_SIZE": "2"}, inference=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(state["CUDA_VISIBLE_DEVICES"], "4")
        self.assertIsNone(state["NPROC_PER_NODE"])
        self.assertIsNone(state["WORLD_SIZE"])

    def test_inference_rejects_accidental_multi_gpu_launch(self):
        result = self.launch({"CUDA_VISIBLE_DEVICES": "4,5"}, inference=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("single-GPU", result.stderr)


class ConfigurationTests(unittest.TestCase):
    def test_smoke_zero_worker_loader_disables_persistence_and_prefetch(self):
        import yaml
        from torch.utils.data import DataLoader

        config = yaml.safe_load((PROJECT_ROOT / "configs/smoke_lora.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["dataloader_num_workers"], 0)
        self.assertIs(config["dataloader_persistent_workers"], False)
        self.assertIsNone(config.get("dataloader_prefetch_factor"))
        loader = DataLoader(
            [0, 1],
            batch_size=1,
            num_workers=config["dataloader_num_workers"],
            persistent_workers=config["dataloader_persistent_workers"],
            prefetch_factor=config.get("dataloader_prefetch_factor"),
        )
        self.assertEqual([batch.item() for batch in loader], [0, 1])

    def test_formal_worker_loader_explicitly_enables_persistence_and_prefetch(self):
        import yaml
        from torch.utils.data import DataLoader

        config = yaml.safe_load((PROJECT_ROOT / "configs/train_lora.yaml").read_text(encoding="utf-8"))
        self.assertGreater(config["dataloader_num_workers"], 0)
        self.assertIs(config["dataloader_persistent_workers"], True)
        self.assertEqual(config["dataloader_prefetch_factor"], 2)
        # Constructor validation does not spawn workers. Avoid iterating here:
        # this regression needs no extra processes, video decoding, or CUDA.
        loader = DataLoader(
            [0, 1],
            batch_size=1,
            num_workers=config["dataloader_num_workers"],
            persistent_workers=config["dataloader_persistent_workers"],
            prefetch_factor=config["dataloader_prefetch_factor"],
        )
        self.assertTrue(loader.persistent_workers)
        self.assertEqual(loader.prefetch_factor, 2)

    def test_preflight_gpu_parser_preserves_order_and_rejects_duplicates(self):
        from kvc_sft.check_env import parse_physical_gpus

        self.assertEqual(parse_physical_gpus("5,4"), [5, 4])
        for value in ("", "0,", "-1", "0, 1", "4,4", "04,4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_physical_gpus(value)

    def test_configs_share_model_lora_and_template_contract(self):
        import yaml
        from kvc_sft.contracts import TEMPLATE_CONTRACT

        for filename in ("train_lora.yaml", "smoke_lora.yaml"):
            with self.subTest(config=filename):
                configuration = yaml.safe_load((PROJECT_ROOT / "configs" / filename).read_text(encoding="utf-8"))
                self.assertEqual(configuration["model"], "Qwen/Qwen3.5-9B")
                self.assertEqual(configuration["model_type"], "qwen3_5")
                self.assertEqual(configuration["tuner_type"], "lora")
                self.assertEqual(configuration["torch_dtype"], "bfloat16")
                self.assertTrue(configuration["freeze_vit"])
                self.assertTrue(configuration["freeze_aligner"])
                self.assertFalse(configuration.get("save_only_model", False))
                for key, expected in TEMPLATE_CONTRACT.items():
                    self.assertEqual(configuration.get(key), expected, key)

    def test_preflight_rejects_legacy_transformers(self):
        from packaging.specifiers import SpecifierSet
        from kvc_sft.check_env import REQUIRED

        supported = SpecifierSet(REQUIRED["transformers"])
        self.assertNotIn("4.57.6", supported)
        self.assertIn("5.9.0", supported)


@unittest.skipUnless(shutil.which("ffprobe"), "launcher validation integration requires ffprobe")
class ValidationRoutingTests(unittest.TestCase):
    """Run the actual first validator, then stop BEFORE GPU preflight/training."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.log = self.root / "argv.jsonl"
        self.python_wrapper = self.root / "python-wrapper"
        self.python_wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "with open(os.environ['KVC_TEST_ARGV_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "module = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == '-m' else ''\n"
            "if module == 'kvc_sft.validate_dataset':\n"
            "    raise SystemExit(subprocess.run([sys.executable, *sys.argv[1:]]).returncode)\n"
            "if module == 'kvc_sft.check_env':\n"
            "    raise SystemExit(37)  # successful routing; intentionally stop before CUDA\n"
            "raise SystemExit(99)\n",
            encoding="utf-8",
        )
        self.python_wrapper.chmod(0o700)
        self.schema = PROJECT_ROOT / "schemas/kvc_action_smoke_v0.schema.json"
        self.smoke = PROJECT_ROOT / "data/kvc_sft_smoke.jsonl"
        formal_row = json.loads(self.smoke.read_text(encoding="utf-8").splitlines()[0])
        formal_row.update({"sample_id": "test_source_clip0", "source_group": "test_source"})
        formal_row["videos"] = [str((self.smoke.parent / path).resolve()) for path in formal_row["videos"]]
        self.formal = self.root / "formal.jsonl"
        self.formal.write_text(json.dumps(formal_row, ensure_ascii=False) + "\n", encoding="utf-8")

    def run_launcher(self, *, smoke_entry=False, dataset=None, updates=None, validation=None):
        environment = dict(os.environ)
        for key in list(environment):
            if key.startswith("KVC_") or key in {
                "CUDA_VISIBLE_DEVICES", "NPROC_PER_NODE", "NNODES", "NODE_RANK", "RANK", "LOCAL_RANK",
                "WORLD_SIZE", "LOCAL_WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT",
            }:
                environment.pop(key)
        environment.update({
            "KVC_SFT_PYTHON": str(self.python_wrapper),
            "KVC_SWIFT_BIN": shutil.which("true"),
            "KVC_TEST_ARGV_LOG": str(self.log),
            "KVC_ACTION_SCHEMA": str(self.schema),
            "KVC_EXPECTED_FEATURES": "16",
            "KVC_FEATURE_SCHEMA": "pass2_smoke_v0",
            "CUDA_VISIBLE_DEVICES": "4,5",
        })
        environment.update(updates or {})
        output = str(self.root / "never-created-training-output")
        if smoke_entry:
            command = ["bash", str(PROJECT_ROOT / "scripts/train_smoke.sh"), output]
        else:
            command = ["bash", str(PROJECT_ROOT / "scripts/train_lora.sh"), str(dataset or self.formal), output]
            if validation:
                command.append(str(validation))
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def test_bundled_smoke_entry_reaches_preflight_without_formal_only_flags(self):
        result, calls = self.run_launcher(smoke_entry=True)
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertIn("global_batch=2", result.stdout)
        self.assertEqual(calls[0][:2], ["-m", "kvc_sft.validate_dataset"])
        self.assertNotIn("--require-absolute-video-paths", calls[0])
        self.assertNotIn("--require-source-group", calls[0])
        self.assertEqual(calls[1][:2], ["-m", "kvc_sft.check_env"])

    def test_user_dataset_keeps_both_strict_flags(self):
        result, calls = self.run_launcher()
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertIn("--require-absolute-video-paths", calls[0])
        self.assertIn("--require-source-group", calls[0])

    def test_smoke_exception_cannot_be_applied_to_user_dataset(self):
        result, calls = self.run_launcher(updates={"KVC_BUNDLED_SMOKE": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("only applies to the exact bundled", result.stderr)
        self.assertEqual(calls, [])

    def test_smoke_exception_cannot_change_schema(self):
        alternative = self.root / "other.schema.json"
        shutil.copyfile(self.schema, alternative)
        result, calls = self.run_launcher(dataset=self.smoke, updates={
            "KVC_BUNDLED_SMOKE": "1", "KVC_ACTION_SCHEMA": str(alternative),
        })
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_smoke_exception_cannot_change_feature_count_or_protocol(self):
        for key, value in (("KVC_EXPECTED_FEATURES", "253"), ("KVC_FEATURE_SCHEMA", "pass2_253_v1")):
            with self.subTest(key=key):
                result, calls = self.run_launcher(dataset=self.smoke, updates={"KVC_BUNDLED_SMOKE": "1", key: value})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_smoke_exception_cannot_add_validation_dataset(self):
        result, calls = self.run_launcher(
            dataset=self.smoke, updates={"KVC_BUNDLED_SMOKE": "1"}, validation=self.formal,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
