from langchain_openai import (
    ChatOpenAI,
)

from jupyter_ai_magics.providers import BaseProvider, EnvAuthStrategy, TextField

class PurdueGenAIStudioProvider(BaseProvider, ChatOpenAI):
    id = "genaistudio"
    name = "PurdueGenAIStudio"
    models = [
        "gemma:latest",
        "llama3.1:latest",
        "llama3.2:latest",
        "llama3.3:70b-instruct-q4_K_M",
    ]
    model_id_key = "model_name"
    pypi_package_deps = ["langchain_openai"]
    auth_strategy = EnvAuthStrategy(name="GENAISTUDIO_API_KEY",  keyword_param="openai_api_key")

    def __init__(self, **kwargs):
        super().__init__(openai_api_base="https://genai.rcac.purdue.edu/api", **kwargs)

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
