"""LLM provider selection for poc-data-seed."""

from __future__ import annotations

import os
from typing import Any, Mapping

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build_llm(temperature: float = 0) -> BaseChatModel:
    """Build the configured chat model from the project environment."""
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model = os.getenv("OPENAI_MODEL")

    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set. Add it to your environment or .env file.")
    if not base_url:
        raise ValueError("OPENAI_BASE_URL is not set. Add it to your environment or .env file.")
    if not model:
        raise ValueError("OPENAI_MODEL is not set. Add it to your environment or .env file.")

    return ChatGroveOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
    )


class ChatGroveOpenAI(ChatOpenAI):
    """OpenAI-compatible chat model authenticated through the configured Grove base URL."""

    def __init__(
        self,
        *,
        api_key: str | SecretStr,
        base_url: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        secret_key = SecretStr(api_key) if isinstance(api_key, str) else api_key
        headers = dict(default_headers or {})
        headers["api-key"] = secret_key.get_secret_value()
        super().__init__(
            api_key=secret_key,
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
            default_headers=headers,
            **kwargs,
        )