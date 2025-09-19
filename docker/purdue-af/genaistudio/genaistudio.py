# ✅ jupyter-ai 3.x style custom provider

from jupyter_ai_magics import BaseProvider               # <- import from top-level (not .providers)
from jupyter_ai_magics.providers import EnvAuthStrategy, TextField
from langchain_openai import ChatOpenAI


class PurdueGenAIStudioProvider(BaseProvider, ChatOpenAI):
    id = "genaistudio"
    name = "PurdueGenAIStudio"

    # Jupyter AI will populate this into the kwarg named by `model_id_key`
    models = [
        "purdue-cms-af",
        # add others if/when exposed by your backend
    ]

    # LangChain's ChatOpenAI now expects `model=` (not `model_name`)
    model_id_key = "model"

    # Jupyter AI will pass the env var to the kwarg named here.
    # ChatOpenAI now expects `api_key=` (not `openai_api_key`)
    auth_strategy = EnvAuthStrategy(
        name="GENAISTUDIO_API_KEY",
        keyword_param="api_key",
    )

    # Newer langchain-openai expects `base_url=` (not `openai_api_base`)
    def __init__(self, **kwargs):
        super().__init__(base_url="https://genai.rcac.purdue.edu/api", **kwargs)

    @classmethod
    def is_api_key_exc(cls, e: Exception):
        """
        Return True if `e` looks like an auth / invalid API key error
        across OpenAI Python SDK versions.
        """
        try:
            import openai
        except Exception:
            return False

        # v1 SDK (modern): openai.AuthenticationError, may have .status_code/.code
        if isinstance(e, getattr(openai, "AuthenticationError", tuple())):
            code = getattr(e, "code", None)
            status = getattr(e, "status_code", None)
            msg = str(e).lower()
            return (
                code == "invalid_api_key"
                or status == 401
                or "invalid api key" in msg
                or "no api key" in msg
            )

        # very old v0 SDK compatibility (json_body shape)
        if hasattr(e, "json_body"):
            err = (getattr(e, "json_body", {}) or {}).get("error", {})
            return err.get("code") == "invalid_api_key"

        return False