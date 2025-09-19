# jupyter_ai v3-compatible provider using LiteLLM
from __future__ import annotations

from jupyter_ai_magics.providers import (
    BaseProvider,
    EnvAuthStrategy,
    TextField,
)

# We will call LiteLLM directly
from litellm import completion, ChatCompletion
from litellm.exceptions import AuthenticationError, RateLimitError, APIError

class PurdueGenAIStudioProvider(BaseProvider):
    """
    Jupyter-AI v3 provider for Purdue GenAI Studio via an OpenAI-compatible endpoint,
    implemented with LiteLLM.
    """
    id = "genaistudio"
    name = "PurdueGenAIStudio"

    # Show your models in the UI the same way as v2
    models = [
        "purdue-cms-af",
        # add others here if you expose more backends via your gateway
    ]

    # In v3 we don't inherit ChatOpenAI, so `model_id_key` can be something generic.
    # Jupyter-AI uses this to read the selected model from the UI/config.
    model_id_key = "model"

    # Swap dependency from LangChain to LiteLLM
    pypi_package_deps = ["litellm"]

    # Keep env-based auth. We’ll pass the API key through to LiteLLM as `api_key`.
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

    # ---- Core wiring for LiteLLM ----

    def _litellm_common_kwargs(self, **kwargs) -> dict:
        """
        Translate Jupyter-AI/provider config to LiteLLM kwargs.
        """
        # Provider UI / config supplies these via kwargs; fall back to defaults/fields
        model = kwargs.get(self.model_id_key) or kwargs.get("model") or self.models[0]
        api_key = kwargs.get("api_key")  # supplied by EnvAuthStrategy
        base_url = kwargs.get("base_url") or "https://genai.rcac.purdue.edu/api"

        # You can pass through temperature, max_tokens, etc. from the UI if desired.
        # Jupyter-AI forwards arbitrary model params in kwargs.
        temperature = kwargs.get("temperature", 0.0)
        max_tokens = kwargs.get("max_tokens", None)

        # LiteLLM "openai" provider against a custom base_url is the right fit here.
        return {
            "model": model,
            "api_key": api_key,
            "base_url": base_url,  # OpenAI-compatible endpoint
            "temperature": temperature,
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
        }

    # Jupyter-AI calls into providers to get a *callable* for chat. For v3 + LiteLLM,
    # we return a thin adapter that uses litellm.completion(ChatCompletion) under the hood.
    def get_chat_client(self, **kwargs):
        """
        Return a callable(messages: list[dict]) -> str that Jupyter-AI can invoke.

        Each `messages` item is an OpenAI-style dict:
        {"role": "system"|"user"|"assistant", "content": "..."}.
        """
        common = self._litellm_common_kwargs(**kwargs)

        def _chat(messages, **call_kwargs) -> str:
            # Merge per-call overrides (e.g., temperature) if Jupyter-AI supplies any.
            params = {**common, **call_kwargs}

            # Use LiteLLM's OpenAI-compatible chat endpoint
            # Response content is always at choices[0].message["content"]
            resp: ChatCompletion = completion(
                messages=messages,
                **params,
            )
            return resp["choices"][0]["message"]["content"]

        return _chat

    # If Jupyter-AI asks for a *streaming* client, provide a generator of tokens.
    def get_chat_stream_client(self, **kwargs):
        """
        Return a callable(messages) -> iterator[str] yielding chunks/tokens.
        """
        common = self._litellm_common_kwargs(**kwargs)

        def _chat_stream(messages, **call_kwargs):
            params = {**common, **call_kwargs, "stream": True}
            for chunk in completion(messages=messages, **params):
                # LiteLLM yields OpenAI-style delta chunks
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0]["delta"]
                    if isinstance(delta, dict):
                        text = delta.get("content") or ""
                    else:
                        text = str(delta or "")
                    if text:
                        yield text

        return _chat_stream

    # Map exception → “invalid key?” so the UI can show the right hint.
    @classmethod
    def is_api_key_exc(cls, e: Exception) -> bool:
        # LiteLLM raises AuthenticationError for 401s
        return isinstance(e, AuthenticationError)