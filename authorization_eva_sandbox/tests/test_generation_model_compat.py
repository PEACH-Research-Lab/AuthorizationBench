from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage1 import (
    _call_reasoning_check,
)
from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
    _call_stage2_step,
)


def _response(text: str = "Reasoning\nPASS") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class GenerationModelCompatibilityTests(unittest.TestCase):
    def _config(self, body: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(body)
        return path

    @patch("litellm.completion")
    def test_gpt5_critic_does_not_receive_temperature_zero(self, completion):
        completion.return_value = _response()
        config = self._config(
            '[llm.gpt5_6_sol]\n'
            'model = "gpt-5.6-sol"\n'
            'api_key = "test"\n'
        )
        _, passed = _call_reasoning_check(
            prompt="check",
            profile_name="gpt5_6_sol",
            config_file=config,
        )
        self.assertTrue(passed)
        self.assertNotIn("temperature", completion.call_args.kwargs)
        self.assertEqual(completion.call_args.kwargs["max_tokens"], 8000)

    @patch("litellm.completion")
    def test_profile_completion_kwargs_are_authoritative(self, completion):
        completion.return_value = _response()
        config = self._config(
            '[llm.custom]\n'
            'model = "custom-model"\n'
            'api_key = "test"\n'
            'completion_kwargs = { temperature = 0.4, top_p = 0.8 }\n'
        )
        _call_reasoning_check(
            prompt="check",
            profile_name="custom",
            config_file=config,
        )
        self.assertEqual(completion.call_args.kwargs["temperature"], 0.4)
        self.assertEqual(completion.call_args.kwargs["top_p"], 0.8)

    @patch("litellm.completion")
    def test_non_gpt5_critic_keeps_deterministic_default(self, completion):
        completion.return_value = _response()
        config = self._config(
            '[llm.custom]\n'
            'model = "custom-model"\n'
            'api_key = "test"\n'
        )
        _call_reasoning_check(
            prompt="check",
            profile_name="custom",
            config_file=config,
        )
        self.assertEqual(completion.call_args.kwargs["temperature"], 0)
        self.assertEqual(completion.call_args.kwargs["max_tokens"], 2000)

    @patch("litellm.completion")
    def test_empty_visible_critic_output_is_infrastructure_error(self, completion):
        completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                reasoning_content="internal reasoning without a final answer",
            ))]
        )
        config = self._config(
            '[llm.gpt5_6_sol]\n'
            'model = "gpt-5.6-sol"\n'
            'api_key = "test"\n'
        )
        with self.assertRaisesRegex(RuntimeError, "no visible verdict"):
            _call_reasoning_check(
                prompt="check",
                profile_name="gpt5_6_sol",
                config_file=config,
            )

    @patch("litellm.completion")
    def test_missing_final_verdict_is_infrastructure_error(self, completion):
        completion.return_value = _response("Reasoning without verdict")
        config = self._config(
            '[llm.custom]\n'
            'model = "custom-model"\n'
            'api_key = "test"\n'
        )
        with self.assertRaisesRegex(RuntimeError, "no PASS/FAIL"):
            _call_reasoning_check(
                prompt="check",
                profile_name="custom",
                config_file=config,
            )

    @patch(
        "authorization_eva_sandbox.task_generation.stages."
        "seed_to_vignette.stage2.generate_vignette"
    )
    def test_empty_stage2_payload_retries_then_stops(self, generate):
        generate.return_value = {}
        config = self._config(
            '[llm.opus_5]\n'
            'model = "openrouter/anthropic/claude-opus-5"\n'
            'api_key = "test"\n'
        )
        with self.assertRaisesRegex(RuntimeError, "failed after retries"):
            _call_stage2_step(
                prompt="generate",
                profile_name="opus_5",
                config_file=config,
                max_tokens=100,
                retries=1,
            )
        self.assertEqual(generate.call_count, 2)
        for call in generate.call_args_list:
            self.assertIsNone(call.kwargs["fallback_profile"])

    @patch("litellm.completion")
    def test_complete_text_keeps_pass_without_json_parse(self, completion):
        from authorization_eva_sandbox.task_generation.shared.llm import complete_text
        completion.return_value = _response("PASS")
        config = self._config(
            '[llm.custom]\n'
            'model = "custom-model"\n'
            'api_key = "test"\n'
        )
        text = complete_text(
            prompt="review",
            profile_name="custom",
            config_file=config,
            max_tokens=8000,
        )
        self.assertEqual(text, "PASS")
        self.assertEqual(
            completion.call_args.kwargs["response_format"],
            {"type": "text"},
        )
        self.assertNotEqual(
            completion.call_args.kwargs["response_format"],
            {"type": "json_object"},
        )

    @patch(
        "authorization_eva_sandbox.task_generation.shared.llm."
        "complete_text_openai_responses",
        return_value="PASS",
    )
    def test_gpt5_text_completion_skips_json_extract(self, openai_text):
        from authorization_eva_sandbox.task_generation.shared.llm import complete_text
        config = self._config(
            '[llm.gpt5_6_sol]\n'
            'model = "gpt-5.6-sol"\n'
            'api_key = "test"\n'
        )
        text = complete_text(
            prompt="review",
            profile_name="gpt5_6_sol",
            config_file=config,
            max_tokens=8000,
        )
        self.assertEqual(text, "PASS")
        openai_text.assert_called_once()
        self.assertIn("Return the requested protocol text only", openai_text.call_args.kwargs["system"])


if __name__ == "__main__":
    unittest.main()
