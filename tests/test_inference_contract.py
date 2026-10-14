"""Test inference boundaries with a fake engine; no weights or GPU are loaded."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from kvc_sft import infer_and_validate
from kvc_sft.contracts import ContractError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CharacterTokenizer:
    eos_token_id = 0
    eos_text = "<|im_end|>"

    def encode(self, text, **kwargs):
        result = []
        for index, part in enumerate(text.split(self.eos_text)):
            if index:
                result.append(self.eos_token_id)
            result.extend(map(ord, part))
        return result

    def decode(self, token_ids, **kwargs):
        return "".join(self.eos_text if token == self.eos_token_id else chr(token) for token in token_ids)


class FakeTemplate:
    """Model ms-swift's relevant decoding behavior without importing a model."""

    non_thinking_prefix = "<think>\n\n</think>\n\n"

    def __init__(self, *, enable_thinking=False, response_prefix=None):
        self.enable_thinking = enable_thinking
        self.response_prefix = response_prefix
        self.tokenizer = CharacterTokenizer()
        self.decode_calls = []

    def _get_response_prefix(self):
        if self.response_prefix is not None:
            return self.response_prefix
        return "<think>\n" if self.enable_thinking else self.non_thinking_prefix

    def decode_generate_ids(self, token_ids, *, first_token=True, skip_special_tokens=False, **kwargs):
        self.decode_calls.append({"first_token": first_token, "skip_special_tokens": skip_special_tokens})
        # Remove only the terminal EOS; preserve internal special tokens and
        # every genuinely generated character, including whitespace/thinking.
        content_ids = token_ids[:-1] if token_ids and token_ids[-1] == self.tokenizer.eos_token_id else token_ids
        decoded = self.tokenizer.decode(content_ids, skip_special_tokens=skip_special_tokens)
        return (self._get_response_prefix() if first_token else "") + decoded


class InferenceContractTests(unittest.TestCase):
    def invoke(self, *, response=None, allow_invalid=False):
        smoke = PROJECT_ROOT / "data/kvc_sft_smoke.jsonl"
        original = json.loads(smoke.read_text(encoding="utf-8").splitlines()[0])
        if response is None:
            response = original["messages"][-1]["content"]
        evidence = {}

        class FakeEngine:
            def __init__(self, model, **kwargs):
                evidence["model"] = model
                evidence["engine_kwargs"] = kwargs
                self.template = FakeTemplate(enable_thinking=True, response_prefix="unexpected")
                evidence["template"] = self.template

            def infer(self, requests, **kwargs):
                evidence["requests"] = requests
                evidence["infer_kwargs"] = kwargs
                token_ids = self.template.tokenizer.encode(response) + [self.template.tokenizer.eos_token_id]
                return [SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=self.template._get_response_prefix() + response),
                        token_ids=token_ids,
                    )],
                    prompt_token_ids=[7] * 714,
                )]

        modules = {
            "torch": SimpleNamespace(bfloat16="mock-bfloat16"),
            "swift.infer_engine": SimpleNamespace(
                InferRequest=lambda **kwargs: SimpleNamespace(**kwargs),
                RequestConfig=lambda **kwargs: SimpleNamespace(**kwargs),
                TransformersEngine=FakeEngine,
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "base"
            base.mkdir()
            adapter = Path(temporary) / "checkpoint-2"
            argv = [
                "infer_and_validate", "--adapter", str(adapter), "--dataset", str(smoke),
                "--schema", str(PROJECT_ROOT / "schemas/kvc_action_smoke_v0.schema.json"),
                "--swift-repo", temporary, "--video-backend", "torchvision",
            ]
            if allow_invalid:
                argv.append("--allow-invalid")
            stdout = io.StringIO()
            with mock.patch("sys.argv", argv), \
                    mock.patch.dict(os.environ), \
                    mock.patch.dict("sys.modules", modules), \
                    mock.patch.object(infer_and_validate, "resolve_adapter", return_value=adapter), \
                    mock.patch.object(infer_and_validate, "verify_contract", return_value=(None, {"resolved_base_path": str(base)})), \
                    contextlib.redirect_stdout(stdout):
                code = infer_and_validate.main()
            report = json.loads(stdout.getvalue())
        return code, report, evidence, original

    def test_generation_never_receives_assistant_ground_truth(self):
        code, report, evidence, original = self.invoke()
        self.assertEqual(code, 0)
        request = evidence["requests"][0]
        self.assertEqual(request.messages, original["messages"][:-1])
        self.assertNotIn("assistant", [message["role"] for message in request.messages])
        self.assertEqual(report["label"], original["messages"][-1]["content"])

    def test_engine_and_request_both_disable_thinking_without_json_prefill(self):
        _, report, evidence, _ = self.invoke()
        self.assertEqual(evidence["engine_kwargs"]["model_type"], "qwen3_5")
        self.assertEqual(evidence["engine_kwargs"]["template_type"], "qwen3_5")
        self.assertFalse(evidence["template"].enable_thinking)
        self.assertIsNone(evidence["template"].response_prefix)
        self.assertEqual(evidence["requests"][0].chat_template_kwargs, {"enable_thinking": False})
        self.assertIsNone(report["response_prefix"])
        self.assertTrue(evidence["infer_kwargs"]["request_config"].return_details)
        self.assertEqual(report["prompt_token_count"], 714)

    def test_raw_output_is_preserved_even_when_json_is_invalid(self):
        raw = "```json\n{\"qp\":33}\n```"
        code, report, _, _ = self.invoke(response=raw)
        self.assertEqual(code, 2)
        self.assertEqual(report["response"], raw)
        self.assertEqual(report["status"], "generated_but_invalid")
        self.assertFalse(report["schema_valid"])
        self.assertIsNone(report["prediction"])
        self.assertIsNone(report["canonical_response"])

    def test_smoke_allow_invalid_does_not_claim_schema_success(self):
        code, report, _, _ = self.invoke(response="not JSON", allow_invalid=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "generated_but_invalid")
        self.assertFalse(report["schema_valid"])
        self.assertIsNotNone(report["validation_error"])

    def test_json_schema_prefix_is_not_silently_reinserted(self):
        code, report, _, _ = self.invoke(response='"qp":33,"alf":1}')
        self.assertEqual(code, 2)
        self.assertEqual(report["response"], '"qp":33,"alf":1}')
        self.assertFalse(report["schema_valid"])

    def test_valid_complete_action_is_canonicalized(self):
        original = json.loads((PROJECT_ROOT / "data/kvc_sft_smoke.jsonl").read_text().splitlines()[0])
        target = original["messages"][-1]["content"]
        spaced = json.dumps(json.loads(target))
        code, report, _, _ = self.invoke(response=spaced)
        self.assertEqual(code, 0)
        self.assertEqual(report["response"], spaced)
        self.assertEqual(report["canonical_response"], target)
        self.assertTrue(report["schema_valid"])

    def test_framework_reconstructed_prefix_is_not_reported_as_model_generation(self):
        code, report, _, original = self.invoke()
        target = original["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertEqual(report["response"], target)
        self.assertEqual(report["swift_response"], FakeTemplate.non_thinking_prefix + target)
        self.assertEqual(report["raw_generated_text_with_special_tokens"], target + CharacterTokenizer.eos_text)
        self.assertEqual(report["non_thinking_context_prefix"], FakeTemplate.non_thinking_prefix)
        self.assertEqual(report["generated_token_ids"][-1], CharacterTokenizer.eos_token_id)

    def test_genuinely_generated_empty_or_nonempty_thinking_is_preserved_and_rejected(self):
        for thinking in (FakeTemplate.non_thinking_prefix, "<think>\nreasoning\n</think>\n\n"):
            with self.subTest(thinking=thinking):
                raw = thinking + '{"schema":"kvc_action_smoke_v0","qp":33}'
                code, report, _, _ = self.invoke(response=raw)
                self.assertEqual(code, 2)
                self.assertEqual(report["response"], raw)
                self.assertEqual(report["swift_response"], FakeTemplate.non_thinking_prefix + raw)
                self.assertFalse(report["schema_valid"])


class GeneratedTokenDecodingTests(unittest.TestCase):
    def choice(self, template, text):
        return SimpleNamespace(
            token_ids=template.tokenizer.encode(text) + [template.tokenizer.eos_token_id],
            message=SimpleNamespace(content=template._get_response_prefix() + text),
        )

    def test_decoding_disables_prefix_reconstruction_and_preserves_special_tokens(self):
        template = FakeTemplate()
        choice = self.choice(template, '{"qp":33}')
        report = infer_and_validate.decode_generated_response(template, choice)
        self.assertEqual(template.decode_calls, [{"first_token": False, "skip_special_tokens": False}])
        self.assertEqual(report["response"], '{"qp":33}')
        self.assertEqual(report["generated_token_ids"], choice.token_ids)

    def test_only_terminal_eos_is_removed_not_internal_special_tokens_or_whitespace(self):
        template = FakeTemplate()
        text = ' \n{"note":"<|im_end|>inside"}\n '
        report = infer_and_validate.decode_generated_response(template, self.choice(template, text))
        self.assertEqual(report["response"], text)
        self.assertEqual(report["raw_generated_text_with_special_tokens"], text + CharacterTokenizer.eos_text)

    def test_missing_generated_token_ids_are_rejected(self):
        template = FakeTemplate()
        choice = SimpleNamespace(message=SimpleNamespace(content="not evidence"))
        with self.assertRaisesRegex(ContractError, "generated token IDs"):
            infer_and_validate.decode_generated_response(template, choice)

    def test_non_list_or_non_integer_token_ids_are_rejected(self):
        template = FakeTemplate()
        for token_ids in (None, "123", (1, 2), [True], [1.5]):
            with self.subTest(token_ids=token_ids):
                choice = SimpleNamespace(token_ids=token_ids, message=SimpleNamespace(content="not evidence"))
                with self.assertRaisesRegex(ContractError, "generated token IDs"):
                    infer_and_validate.decode_generated_response(template, choice)

    def test_framework_content_that_disagrees_with_token_evidence_is_rejected(self):
        template = FakeTemplate()
        choice = self.choice(template, '{"qp":33}')
        choice.message.content = template.non_thinking_prefix + '{"qp":31}'
        with self.assertRaisesRegex(ContractError, "does not match"):
            infer_and_validate.decode_generated_response(template, choice)


if __name__ == "__main__":
    unittest.main()
