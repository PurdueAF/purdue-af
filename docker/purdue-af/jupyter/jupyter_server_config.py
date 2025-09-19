# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
# mypy: ignore-errors
import os
import stat
import subprocess

from jupyter_core.paths import jupyter_data_dir

c = get_config()  # noqa: F821
c.ServerApp.ip = "0.0.0.0"
c.ServerApp.open_browser = False
c.ServerApp.disable_check_xsrf = True

# to output both image/svg+xml and application/pdf plot formats in the notebook file
c.InlineBackend.figure_formats = {"png", "jpeg", "svg", "pdf"}

# https://github.com/jupyter/notebook/issues/3130
c.FileContentsManager.delete_to_trash = False

# Generate a self-signed certificate
OPENSSL_CONFIG = """\
[req]
distinguished_name = req_distinguished_name
[req_distinguished_name]
"""
if "GEN_CERT" in os.environ:
    dir_name = jupyter_data_dir()
    pem_file = os.path.join(dir_name, "notebook.pem")
    os.makedirs(dir_name, exist_ok=True)

    # Generate an openssl.cnf file to set the distinguished name
    cnf_file = os.path.join(os.getenv("CONDA_DIR", "/usr/lib"), "ssl", "openssl.cnf")
    if not os.path.isfile(cnf_file):
        with open(cnf_file, "w") as fh:
            fh.write(OPENSSL_CONFIG)

    # Generate a certificate if one doesn't exist on disk
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-new",
            "-newkey=rsa:2048",
            "-days=365",
            "-nodes",
            "-x509",
            "-subj=/C=XX/ST=XX/L=XX/O=generated/CN=generated",
            f"-keyout={pem_file}",
            f"-out={pem_file}",
        ]
    )
    # Restrict access to the file
    os.chmod(pem_file, stat.S_IRUSR | stat.S_IWUSR)
    c.ServerApp.certfile = pem_file

# Change default umask for all subprocesses of the notebook server if set in
# the environment
if "NB_UMASK" in os.environ:
    os.umask(int(os.environ["NB_UMASK"], 8))

c.AiExtension.help_message_template = """
Hello! I am {persona_name}, a JupyterLab AI assistant.

I use open LLM models served by <a href="https://www.rcac.purdue.edu/knowledge/genaistudio" target="_blank">Purdue GenAI Studio</a>.
I also have the knowledge of <a href="https://analysis-facility.physics.purdue.edu/" target="_blank">Purdue AF documentation</a>.

<a href="https://www.rcac.purdue.edu/knowledge/genaistudio/api" target="_blank">How to obtain API key</a>

**WARNING: do not rely exclusively on AI responses, as models may hallucinate.**
"""

# Keep your custom provider & model as the default for the Chat UI (and Magics, optionally)
c.AiExtension.default_language_model = "genaistudio:purdue-cms-af"
c.AiMagics.default_language_model = "genaistudio:purdue-cms-af"  # optional but handy

# Restrict the visible providers to your custom one
c.AiExtension.allowed_providers = ["genaistudio"]

# # If the env var is set, pre-wire it so users don't have to paste it into the UI
# c.AiExtension.default_api_keys = {
#     "GENAISTUDIO_API_KEY": os.environ.get("GENAISTUDIO_API_KEY", "")
# }
