from .providers import LLMClient, LLMError, LLMMessage, LLMRateLimitError, SUPPORTED_LLM_TYPES, create_llm_client

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMRateLimitError",
    "SUPPORTED_LLM_TYPES",
    "create_llm_client",
]


def __getattr__(name: str):
    """Lazy attribute access for the translation submodule.

    ``app.llm.translation`` imports ``PipelineError`` from ``app.pipeline``,
    which in turn re-exports from this module — a deferred lookup here avoids
    triggering that cycle at ``app.llm`` import time.  Users can still do
    ``from app.llm.translation import ChunkedTranslator`` directly.
    """
    if name == "translation":
        from . import translation as _translation

        return _translation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
