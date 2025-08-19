from jupyter_ai_magics.providers import (BaseProvider, EnvAuthStrategy,
                                         TextField)
from langchain_openai import ChatOpenAI


class PurdueGenAIStudioProvider(BaseProvider, ChatOpenAI):
    id = "genaistudio"
    name = "PurdueGenAIStudio"
    models = [
        "purdue-cms-af",
    ]
    model_id_key = "model"
    pypi_package_deps = ["langchain_openai"]
    auth_strategy = EnvAuthStrategy(
        name="GENAISTUDIO_API_KEY", keyword_param="api_key"
    )

    def __init__(self, **kwargs):
        super().__init__(base_url="https://genai.rcac.purdue.edu/api", **kwargs)

    @classmethod
    def is_api_key_exc(cls, e: Exception):
        """
        Determine if the exception is an OpenAI API key error.
        """
        import openai

        if isinstance(e, openai.AuthenticationError):
            error_details = e.json_body.get("error", {})
            return error_details.get("code") == "invalid_api_key"
        return False
