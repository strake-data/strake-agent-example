import os
import logging
from functools import lru_cache
from typing import Union, Literal

from dotenv import load_dotenv
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider

# Load environment variables from .env if present
load_dotenv()

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("strake_agent")

# --- Constants & Configuration ---
AUTO_COMPACT_THRESHOLD = 80_000
INTRA_RUN_TOOL_RESULT_CAP = 500
KEEP_RECENT_FULL = 2
TOOL_RESULT_PREVIEW_LEN = 500
MIN_COMPRESSIBLE_LEN = 100
MAX_CONVERSATION_CHARS = 80_000
MAX_TOOL_OUTPUT_CHARS = 2_000
TASK_REMINDER_ROUNDS = 3
SCRIPT_SQL_CALL_LIMIT = 20
SCRIPT_SHORT_OUTPUT_WARN_CHARS = 10
SCRIPT_EFFICIENCY_WARNING_CALL = 8
SCRIPT_EFFICIENCY_CRITICAL_CALL = 12

STRAKE_SSE_URL = os.getenv("STRAKE_SSE_URL", "http://127.0.0.1:8001/sse")

# Provider settings
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "google").lower()
MODEL_NAME = os.getenv("MODEL_NAME")

# Default model settings (mostly for Gemini)
GEMINI_SETTINGS = GoogleModelSettings(google_thinking_config={"thinking_level": "low"})


def get_model(purpose: Literal["agent", "summarizer"] = "agent") -> Model:
    """Initialize and return a PydanticAI model based on environment config.

    Args:
        purpose: Whether the model is for the main agent or the summarizer.

    Returns:
        A Model instance (GoogleModel, OpenAIModel, or AnthropicModel).

    Raises:
        RuntimeError: If required API keys are missing or provider is unknown.
    """
    provider_type = os.getenv("MODEL_PROVIDER", "google").lower()
    model_name = os.getenv("MODEL_NAME")

    if provider_type == "google":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set for google provider")
        # Default to gemini-3-flash-preview if not specified
        name = model_name or "gemini-3-flash-preview"
        return GoogleModel(name, provider=GoogleProvider(api_key=api_key))

    elif provider_type == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set for openai provider")
        # Default to gpt-4o if not specified
        name = model_name or "gpt-5.3-codex"
        return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key))

    elif provider_type == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set for anthropic provider")
        # Default to claude-3-5-sonnet-latest if not specified
        name = model_name or "claude-4-5-sonnet-latest"
        return AnthropicModel(name, provider=AnthropicProvider(api_key=api_key))

    else:
        raise RuntimeError(f"Unknown model provider: {provider_type}")


# Fallback for code expecting GEMINI_SETTINGS
def get_model_settings() -> Union[GoogleModelSettings, None]:
    """Return provider-specific model settings if any."""
    provider_type = os.getenv("MODEL_PROVIDER", "google").lower()
    if provider_type == "google":
        return GEMINI_SETTINGS
    return None
