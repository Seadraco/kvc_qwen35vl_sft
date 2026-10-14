"""CPU-only loss-mask regression tests; no model download or CUDA required."""

from __future__ import annotations

import unittest

from kvc_sft.audit_token_lengths import audit_assistant_labels
from kvc_sft.contracts import ContractError, TEMPLATE_CONTRACT, sft_template_kwargs


class CharacterTokenizer:
    """Intentionally simple tokenizer, so mask failures cannot hide in BPE."""

    def decode(self, tokens, **kwargs):
        return "".join(chr(token) for token in tokens)


class AssistantLabelTests(unittest.TestCase):
    target = '{"schema":"kvc_action_smoke_v0","qp":33}'
    non_thinking = "<think>\n\n</think>\n\n"
    suffix = "<|im_end|>\n"

    def fixture(self):
        prompt = "system user <|video_pad|> features\n<|im_start|>assistant\n" + self.non_thinking
        answer = self.target + self.suffix
        return {
            "input_ids": list(map(ord, prompt + answer)),
            "labels": [-100] * len(prompt) + list(map(ord, answer)),
        }

    def audit(self, encoded):
        return audit_assistant_labels(
            encoded,
            CharacterTokenizer(),
            self.target,
            non_thinking_prefix=self.non_thinking,
            suffix=self.suffix,
        )

    def test_full_json_and_end_of_turn_are_supervised(self):
        result = self.audit(self.fixture())
        self.assertEqual(result["supervised_text"], self.target + self.suffix)
        self.assertEqual(result["supervised_tokens"], len(self.target + self.suffix))
        self.assertTrue(result["assistant_only"])
        self.assertTrue(result["empty_think_masked"])

    def test_empty_labels_are_rejected(self):
        encoded = self.fixture()
        encoded["labels"] = [-100] * len(encoded["labels"])
        with self.assertRaisesRegex(ContractError, "no supervised tokens"):
            self.audit(encoded)

    def test_prompt_supervision_is_rejected(self):
        encoded = self.fixture()
        encoded["labels"][3] = encoded["input_ids"][3]
        with self.assertRaisesRegex(ContractError, "contiguous final assistant span"):
            self.audit(encoded)

    def test_empty_think_loss_is_rejected(self):
        encoded = self.fixture()
        first = encoded["labels"].index(ord("{"))
        for index in range(first - len(self.non_thinking), first):
            encoded["labels"][index] = encoded["input_ids"][index]
        with self.assertRaisesRegex(ContractError, "complete target JSON"):
            self.audit(encoded)

    def test_masked_schema_prefix_is_rejected(self):
        encoded = self.fixture()
        first = encoded["labels"].index(ord("{"))
        prefix_length = len('{"schema":"kvc_action_smoke_v0",')
        encoded["labels"][first:first + prefix_length] = [-100] * prefix_length
        with self.assertRaisesRegex(ContractError, "complete target JSON"):
            self.audit(encoded)

    def test_truncated_target_is_rejected(self):
        encoded = self.fixture()
        encoded["input_ids"] = encoded["input_ids"][:-15]
        encoded["labels"] = encoded["labels"][:-15]
        with self.assertRaisesRegex(ContractError, "complete target JSON"):
            self.audit(encoded)

    def test_shifted_labels_are_rejected(self):
        encoded = self.fixture()
        encoded["labels"][-2] = 123
        with self.assertRaisesRegex(ContractError, "unshifted input_ids"):
            self.audit(encoded)

    def test_thinking_inference_prefix_is_rejected(self):
        encoded = self.fixture()
        first = encoded["labels"].index(ord("{"))
        encoded["input_ids"][first - 3] = ord("x")
        with self.assertRaisesRegex(ContractError, "empty non-thinking prefix"):
            self.audit(encoded)

    def test_hole_in_target_supervision_is_rejected(self):
        encoded = self.fixture()
        encoded["labels"][-4] = -100
        with self.assertRaisesRegex(ContractError, "contiguous final assistant span"):
            self.audit(encoded)


class TemplateContractTests(unittest.TestCase):
    def test_training_inference_and_audit_use_one_contract(self):
        self.assertEqual(TEMPLATE_CONTRACT["template"], "qwen3_5")
        self.assertFalse(TEMPLATE_CONTRACT["enable_thinking"])
        self.assertTrue(TEMPLATE_CONTRACT["add_non_thinking_prefix"])
        self.assertEqual(TEMPLATE_CONTRACT["loss_scale"], "ignore_empty_think")
        self.assertIsNone(TEMPLATE_CONTRACT["response_prefix"])

    def test_get_template_kwargs_have_python_api_spelling(self):
        kwargs = sft_template_kwargs()
        self.assertEqual(kwargs.pop("template_type"), "qwen3_5")
        self.assertNotIn("template", kwargs)
        self.assertEqual(kwargs, {key: value for key, value in TEMPLATE_CONTRACT.items() if key != "template"})

    def test_mutating_kwargs_does_not_change_contract(self):
        kwargs = sft_template_kwargs()
        kwargs["enable_thinking"] = True
        self.assertFalse(TEMPLATE_CONTRACT["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
