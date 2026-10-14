"""Tiny CPU safetensors prove that training verification checks actual weights."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np
from safetensors.numpy import save_file

from kvc_sft.contracts import ContractError
from kvc_sft.inspect_run import inspect_adapter_tensors


class AdapterTensorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.adapter = Path(self.temporary.name)

    def save(self, b_value, *, a_value=0.1, include_b=True):
        tensors = {"base_model.model.proj.lora_A.weight": np.full((2, 4), a_value, dtype=np.float32)}
        if include_b:
            tensors["base_model.model.proj.lora_B.weight"] = np.full((4, 2), b_value, dtype=np.float32)
        save_file(tensors, self.adapter / "adapter_model.safetensors")

    def test_finite_nonzero_lora_b_demonstrates_an_update(self):
        self.save(0.001)
        report = inspect_adapter_tensors(self.adapter)
        self.assertTrue(report["all_tensors_finite"])
        self.assertEqual(report["tensor_count"], 2)
        self.assertEqual(report["lora_B_tensors"], 1)
        self.assertEqual(report["nonzero_B_tensors"], 1)
        self.assertGreater(report["max_abs_B"], 0)

    def test_unchanged_zero_initialized_b_is_rejected(self):
        self.save(0.0)
        with self.assertRaisesRegex(ContractError, "no nonzero LoRA B"):
            inspect_adapter_tensors(self.adapter)

    def test_nan_or_infinite_b_is_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaisesRegex(ContractError, "NaN/Inf"):
                    inspect_adapter_tensors(self.adapter)

    def test_nonfinite_other_adapter_tensors_are_also_rejected(self):
        self.save(0.001, a_value=float("nan"))
        with self.assertRaisesRegex(ContractError, "NaN/Inf"):
            inspect_adapter_tensors(self.adapter)

    def test_no_b_tensor_cannot_claim_a_learned_update(self):
        self.save(0, include_b=False)
        with self.assertRaisesRegex(ContractError, "no nonzero LoRA B"):
            inspect_adapter_tensors(self.adapter)

    def test_missing_safetensors_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "requires safetensors"):
            inspect_adapter_tensors(self.adapter)


if __name__ == "__main__":
    unittest.main()
