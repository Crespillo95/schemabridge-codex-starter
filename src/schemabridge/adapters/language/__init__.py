"""Natural-language adapters."""

from schemabridge.adapters.language.fake import FakeIntentParser
from schemabridge.adapters.language.openai import OpenAIIntentParser

__all__ = ["FakeIntentParser", "OpenAIIntentParser"]
