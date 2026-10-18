"""Model provider selection.

There is deliberately no automatic fallback between providers: a task runs on the
provider it was started with, and if that provider fails the task fails with a clear
error. In particular a local-model task is never retried on a cloud API.
"""

from __future__ import annotations

from ..config import Settings
from .base import LocalModeViolation, ModelConfigError, ModelError, ModelProvider

PROVIDERS = ("anthropic", "ollama", "openai_compatible")


def build_provider(settings: Settings, name: str | None = None) -> ModelProvider:
    name = name or settings.model_provider
    if name == "anthropic":
        if settings.local_only:
            raise LocalModeViolation(
                "LOCAL_ONLY=true: the Anthropic API is a cloud service, so it is disabled. "
                "Use MODEL_PROVIDER=ollama or openai_compatible with a local endpoint."
            )
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "ollama":
        from .local_providers import OllamaProvider

        provider = OllamaProvider(settings)
    elif name == "openai_compatible":
        from .local_providers import OpenAICompatProvider

        provider = OpenAICompatProvider(settings)
    else:
        raise ModelConfigError(f"Unknown model provider '{name}'. Choose one of: {', '.join(PROVIDERS)}")
    if settings.local_only and not provider.is_local:
        raise LocalModeViolation(
            f"LOCAL_ONLY=true but the {name} endpoint/model is not on this machine or your private network. "
            "Prompts will not be sent there."
        )
    return provider


def configured_providers(settings: Settings) -> list[dict]:
    """Providers the user could switch to in Settings, with whether they look configured."""
    out = []
    out.append(
        {
            "name": "anthropic",
            "label": f"Claude API ({settings.anthropic_model})",
            "configured": settings.anthropic_api_key is not None,
            "local": False,
            "allowed": not settings.local_only,
        }
    )
    from ..config import is_local_endpoint
    from .local_providers import is_ollama_cloud_model

    ollama_local = is_local_endpoint(settings.ollama_base_url) and not is_ollama_cloud_model(settings.ollama_model)
    out.append(
        {
            "name": "ollama",
            "label": f"Ollama ({settings.ollama_model})",
            "configured": True,
            "local": ollama_local,
            "allowed": ollama_local or not settings.local_only,
        }
    )
    compat_local = bool(settings.openai_base_url) and is_local_endpoint(settings.openai_base_url or "")
    out.append(
        {
            "name": "openai_compatible",
            "label": f"OpenAI-compatible ({settings.openai_model or 'not set'})",
            "configured": bool(settings.openai_base_url and settings.openai_model),
            "local": compat_local,
            "allowed": compat_local or not settings.local_only,
        }
    )
    return out


__all__ = ["build_provider", "configured_providers", "ModelProvider", "ModelError", "ModelConfigError", "LocalModeViolation"]
