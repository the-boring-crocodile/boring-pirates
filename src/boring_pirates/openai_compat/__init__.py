"""OpenAI-compatible provider folder (see common/providers.py)."""
from .provider import NAME, OpenAICompatProvider as Provider, client, inspect, settings, validate_endpoint

__all__ = ["NAME", "Provider", "client", "inspect", "settings", "validate_endpoint"]
