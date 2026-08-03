"""Tests for the configurable LLM sampling / retry parameters.

Covers:
- Each provider client's __init__ honors the new optional kwargs
  (temperature, max_tokens, frequency_penalty, presence_penalty, http_max_retries)
- `None` values fall back to the module-level DEFAULT_LLM_* constants
- create_llm_client passes kwargs through to the right client subclass
- The legacy call site (no kwargs) still works and uses the previous
  hardcoded behavior (T=0.3, max_tokens=8192, retries=3)
- _migrate_translation_config backfills missing fields on old config rows
  and clamps invalid values
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.providers import (
    DEFAULT_LLM_FREQUENCY_PENALTY,
    DEFAULT_LLM_HTTP_MAX_RETRIES,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_PRESENCE_PENALTY,
    DEFAULT_LLM_TEMPERATURE,
    AnthropicLLMClient,
    OllamaLLMClient,
    OpenAICompatibleLLMClient,
    OpenAIResponsesLLMClient,
    create_llm_client,
)
from app.store import _migrate_translation_config


class OpenAICompatibleClientConfigTests(unittest.TestCase):
    def test_defaults_applied_when_kwargs_omitted(self) -> None:
        c = OpenAICompatibleLLMClient("http://x", "k", "m", 30, requires_api_key=False)
        self.assertEqual(c.temperature, DEFAULT_LLM_TEMPERATURE)
        self.assertEqual(c.max_tokens, DEFAULT_LLM_MAX_TOKENS)
        self.assertEqual(c.frequency_penalty, DEFAULT_LLM_FREQUENCY_PENALTY)
        self.assertEqual(c.presence_penalty, DEFAULT_LLM_PRESENCE_PENALTY)
        self.assertEqual(c.http_max_retries, DEFAULT_LLM_HTTP_MAX_RETRIES)

    def test_explicit_kwargs_override_defaults(self) -> None:
        c = OpenAICompatibleLLMClient(
            "http://x", "k", "m", 30, requires_api_key=False,
            temperature=0.7, max_tokens=4096, frequency_penalty=0.5,
            presence_penalty=0.2, http_max_retries=5,
        )
        self.assertEqual(c.temperature, 0.7)
        self.assertEqual(c.max_tokens, 4096)
        self.assertEqual(c.frequency_penalty, 0.5)
        self.assertEqual(c.presence_penalty, 0.2)
        self.assertEqual(c.http_max_retries, 5)


class OpenAIResponsesClientConfigTests(unittest.TestCase):
    def test_defaults_applied(self) -> None:
        c = OpenAIResponsesLLMClient("http://x", "k", "m", 30)
        self.assertEqual(c.temperature, DEFAULT_LLM_TEMPERATURE)
        self.assertEqual(c.max_tokens, DEFAULT_LLM_MAX_TOKENS)
        self.assertEqual(c.http_max_retries, DEFAULT_LLM_HTTP_MAX_RETRIES)

    def test_explicit_kwargs(self) -> None:
        c = OpenAIResponsesLLMClient(
            "http://x", "k", "m", 30,
            temperature=0.9, max_tokens=2048, http_max_retries=4,
        )
        self.assertEqual(c.temperature, 0.9)
        self.assertEqual(c.max_tokens, 2048)
        self.assertEqual(c.http_max_retries, 4)


class AnthropicClientConfigTests(unittest.TestCase):
    def test_defaults_applied(self) -> None:
        c = AnthropicLLMClient("http://api.anthropic.com", "k", "claude-3", 30)
        self.assertEqual(c.temperature, DEFAULT_LLM_TEMPERATURE)
        self.assertEqual(c.max_tokens, DEFAULT_LLM_MAX_TOKENS)

    def test_explicit_kwargs(self) -> None:
        c = AnthropicLLMClient(
            "http://api.anthropic.com", "k", "claude-3", 30,
            temperature=0.2, max_tokens=1024,
        )
        self.assertEqual(c.temperature, 0.2)
        self.assertEqual(c.max_tokens, 1024)


class OllamaClientConfigTests(unittest.TestCase):
    def test_temperature_passed(self) -> None:
        c = OllamaLLMClient("http://localhost:11434", "", "llama3", 60, temperature=0.5)
        self.assertEqual(c.temperature, 0.5)
        self.assertEqual(c.max_tokens, DEFAULT_LLM_MAX_TOKENS)


class CreateLLMClientPassthroughTests(unittest.TestCase):
    def test_openai_chat_passes_all_kwargs(self) -> None:
        c = create_llm_client(
            "openai-chat", "http://x", "k", "m", 30,
            temperature=0.6, max_tokens=2048, frequency_penalty=0.3,
            presence_penalty=0.4, http_max_retries=2,
        )
        self.assertIsInstance(c, OpenAICompatibleLLMClient)
        self.assertEqual(c.temperature, 0.6)
        self.assertEqual(c.max_tokens, 2048)
        self.assertEqual(c.frequency_penalty, 0.3)
        self.assertEqual(c.presence_penalty, 0.4)
        self.assertEqual(c.http_max_retries, 2)

    def test_lmstudio_uses_openai_compatible(self) -> None:
        c = create_llm_client("lmstudio", "http://localhost:1234", "", "model", 60, temperature=0.4)
        self.assertIsInstance(c, OpenAICompatibleLLMClient)
        self.assertEqual(c.temperature, 0.4)
        self.assertFalse(c.requires_api_key)

    def test_legacy_call_without_kwargs_uses_defaults(self) -> None:
        """The pre-existing 5-arg call site must keep working with the previous
        hardcoded behavior (T=0.3, max_tokens=8192, retries=3) so we don't
        silently change translation output for users on old code."""
        c = create_llm_client("openai-chat", "http://x", "k", "m", 30)
        self.assertEqual(c.temperature, 0.3)
        self.assertEqual(c.max_tokens, 8192)
        self.assertEqual(c.http_max_retries, 3)

    def test_anthropic_falls_back(self) -> None:
        c = create_llm_client("anthropic", "http://api.anthropic.com", "k", "claude-3", 30)
        self.assertIsInstance(c, AnthropicLLMClient)
        self.assertEqual(c.temperature, 0.3)
        self.assertEqual(c.max_tokens, 8192)


class MigrateTranslationConfigTests(unittest.TestCase):
    def test_backfills_missing_fields_with_previous_hardcoded_defaults(self) -> None:
        cfg = {"enabled": True, "api_key": "k", "model": "m"}
        _migrate_translation_config(cfg)
        self.assertEqual(cfg["temperature"], 0.3)
        self.assertEqual(cfg["max_tokens"], 8192)
        self.assertEqual(cfg["frequency_penalty"], 1.2)
        self.assertEqual(cfg["presence_penalty"], 0.8)
        self.assertEqual(cfg["http_max_retries"], 3)

    def test_preserves_explicit_values(self) -> None:
        cfg = {
            "temperature": 0.7,
            "max_tokens": 4096,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "http_max_retries": 5,
        }
        _migrate_translation_config(cfg)
        self.assertEqual(cfg["temperature"], 0.7)
        self.assertEqual(cfg["max_tokens"], 4096)
        self.assertEqual(cfg["frequency_penalty"], 0.0)
        self.assertEqual(cfg["presence_penalty"], 0.0)
        self.assertEqual(cfg["http_max_retries"], 5)

    def test_clamps_invalid_values(self) -> None:
        cfg = {
            "temperature": "hot",  # not a number → default
            "max_tokens": -1,      # <= 0 → default
            "frequency_penalty": None,  # not a number → default
            "presence_penalty": object(),  # not a number → default
            "http_max_retries": -3,  # < 0 → default
        }
        _migrate_translation_config(cfg)
        self.assertEqual(cfg["temperature"], 0.3)
        self.assertEqual(cfg["max_tokens"], 8192)
        self.assertEqual(cfg["frequency_penalty"], 1.2)
        self.assertEqual(cfg["presence_penalty"], 0.8)
        self.assertEqual(cfg["http_max_retries"], 3)

    def test_accepts_integer_zero_for_temperature(self) -> None:
        cfg = {"temperature": 0, "max_tokens": 1024, "frequency_penalty": 0, "presence_penalty": 0, "http_max_retries": 0}
        _migrate_translation_config(cfg)
        # 0 is a valid value for these numeric params — must not be replaced
        self.assertEqual(cfg["temperature"], 0)
        self.assertEqual(cfg["max_tokens"], 1024)
        self.assertEqual(cfg["frequency_penalty"], 0)
        self.assertEqual(cfg["presence_penalty"], 0)
        self.assertEqual(cfg["http_max_retries"], 0)

    def test_no_op_on_empty(self) -> None:
        cfg: dict = {}
        _migrate_translation_config(cfg)
        self.assertEqual(cfg["temperature"], 0.3)
        self.assertEqual(cfg["max_tokens"], 8192)
        self.assertEqual(cfg["frequency_penalty"], 1.2)
        self.assertEqual(cfg["presence_penalty"], 0.8)
        self.assertEqual(cfg["http_max_retries"], 3)


if __name__ == "__main__":
    unittest.main()
