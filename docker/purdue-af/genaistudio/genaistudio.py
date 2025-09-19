# jupyter_ai v3-compatible provider using LiteLLM
from __future__ import annotations

from typing import AsyncIterator, Callable, Dict, Iterator, List

from jupyter_ai_magics.providers import (
    BaseProvider,
    EnvAuthStrategy,
    TextField,
)

# LiteLLM sync + async
from litellm import completion, acompletion, ChatCompletion
from litellm.exceptions import AuthenticationError, RateLimitError, APIError
import litellm


class PurdueGenAIStudioProvider(BaseProvider):
    """
    Jupyter-AI v3 provider for Purdue GenAI Studio via an OpenAI-compatible endpoint,
    implemented with LiteLLM.
    """
    id = "genaistudio"
    name = "PurdueGenAIStudio"

    # Models displayed in the UI
    models = [
        "purdue-cms-af",
        # add others you expose via your gateway
        # e.g. "llama3.1:70b", "qwen2.5:7b", etc.
    ]

    # Jupyter-AI reads the selected model from this key
    model_id_key = "model"

    # Swap dependency from LangChain to LiteLLM
    pypi_package_deps = ["litellm"]

    # Keep env-based auth. We'll pass the API key through to LiteLLM as `api_key`.
    auth_strategy = EnvAuthStrategy(
        name="GENAISTUDIO_API_KEY",
        keyword_param="api_key",
    )

    # Optional: expose a configurable Base URL in the UI (handy for staging/prod)
    fields = [
        TextField(
            name="base_url",
            label="Base API URL (OpenAI-compatible)",
            default="https://genai.rcac.purdue.edu/api",
            required=False,
            help="Your Purdue GenAI Studio OpenAI-compatible HTTP endpoint.",
        )
    ]

    # -------- Helpers --------

    @classmethod
    def _normalize_model(cls, model_in: str) -> str:
        """
        Convert 'genaistudio:purdue-cms-af' -> 'purdue-cms-af'
        (LiteLLM expects either provider/model or just model; our provider id isn't a LiteLLM provider.)
        """
        if ":" in model_in:
            return model_in.split(":", 1)[1]
        return model_in

    @classmethod
    def _ensure_model_aliases(cls):
        """
        Register aliases so passing 'genaistudio:<model>' works in LiteLLM too.
        Maps -> 'openai/<model>' which uses the OpenAI-compatible path.
        """
        if getattr(litellm, "model_alias_map", None) is None:
            litellm.model_alias_map = {}
        for m in cls.models:
            litellm.model_alias_map.setdefault(f"{cls.id}:{m}", f"openai/{m}")

    def _litellm_common_kwargs(self, **kwargs) -> Dict:
        """
        Translate Jupyter-AI/provider config to LiteLLM kwargs.
        """
        # Provider UI / config supplies these via kwargs; fall back to defaults/fields
        raw_model = kwargs.get(self.model_id_key) or kwargs.get("model") or self.models[0]
        model = self._normalize_model(raw_model)

        api_key = kwargs.get("api_key")  # supplied by EnvAuthStrategy
        base_url = kwargs.get("base_url") or "https://genai.rcac.purdue.edu/api"

        # You can pass through temperature, max_tokens, etc. from the UI if desired.
        # Jupyter-AI forwards arbitrary model params in kwargs.
        temperature = kwargs.get("temperature", 0.0)
        max_tokens = kwargs.get("max_tokens", None)

        # Make it explicit that we're using an OpenAI-compatible endpoint
        common = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,  # OpenAI-compatible endpoint
            "custom_llm_provider": "openai",
            "temperature": temperature,
        }
        if max_tokens is not None:
            common["max_tokens"] = max_tokens

        # If your gateway needs extra headers, you can uncomment and edit:
        # common["headers"] = {"X-Whatever": "value"}

        return common

    # -------- Sync clients (Chat + Streaming) --------

    def get_chat_client(self, **kwargs) -> Callable[[List[Dict], Dict], str]:
        """
        Return a callable(messages: list[dict], **call_kwargs) -> str
        """
        self._ensure_model_aliases()
        common = self._litellm_common_kwargs(**kwargs)

        def _chat(messages: List[Dict], **call_kwargs) -> str:
            params = {**common, **call_kwargs}
            resp: ChatCompletion = completion(messages=messages, **params)
            return resp["choices"][0]["message"]["content"]

        return _chat

    def get_chat_stream_client(self, **kwargs) -> Callable[[List[Dict], Dict], Iterator[str]]:
        """
        Return a callable(messages: list[dict], **call_kwargs) -> iterator[str]
        """
        self._ensure_model_aliases()
        common = self._litellm_common_kwargs(**kwargs)

        def _chat_stream(messages: List[Dict], **call_kwargs) -> Iterator[str]:
            params = {**common, **call_kwargs, "stream": True}
            for chunk in completion(messages=messages, **params):
                # LiteLLM yields OpenAI-style delta chunks
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0].get("delta") or {}
                    text = ""
                    if isinstance(delta, dict):
                        text = delta.get("content") or ""
                    elif delta:
                        text = str(delta)
                    if text:
                        yield text

        return _chat_stream

    # -------- Async clients (Chat + Streaming) --------
    # Some Jupyter-AI personas (e.g., Jupyternaut) call litellm.acompletion under the hood.

    def get_async_chat_client(self, **kwargs):
        """
        Return an async callable(messages: list[dict], **call_kwargs) -> str
        """
        self._ensure_model_aliases()
        common = self._litellm_common_kwargs(**kwargs)

        async def _achat(messages: List[Dict], **call_kwargs) -> str:
            params = {**common, **call_kwargs}
            resp = await acompletion(messages=messages, **params)
            return resp["choices"][0]["message"]["content"]

        return _achat

    def get_async_chat_stream_client(self, **kwargs):
        """
        Return an async callable(messages: list[dict], **call_kwargs) -> AsyncIterator[str]
        """
        self._ensure_model_aliases()
        common = self._litellm_common_kwargs(**kwargs)

        async def _achat_stream(messages: List[Dict], **call_kwargs) -> AsyncIterator[str]:
            params = {**common, **call_kwargs, "stream": True}
            async for chunk in acompletion(messages=messages, **params):
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0].get("delta") or {}
                    text = ""
                    if isinstance(delta, dict):
                        text = delta.get("content") or ""
                    elif delta:
                        text = str(delta)
                    if text:
                        yield text

        return _achat_stream

    # Map exception → “invalid key?” so the UI can show the right hint.
    @classmethod
    def is_api_key_exc(cls, e: Exception) -> bool:
        # LiteLLM raises AuthenticationError for 401s
        return isinstance(e, AuthenticationError)