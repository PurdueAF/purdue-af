"""Test helpers for standalone-script tests.

jupyter_server_config.py is exec'd by `jupyter server` with `get_config()`
in scope — replicated here with the shared ConfigSink, so the file under
test is byte-identical to what ships in the image.
"""

from pathlib import Path

from common import REPO, ConfigSink

AF_IMAGE = REPO / "docker" / "purdue-af"
JUPYTER_CONFIG = AF_IMAGE / "jupyter" / "jupyter_server_config.py"
HOOKS_IN_IMAGE = "/usr/local/bin/before-notebook.d/"


def sandboxed(path, rewrites):
    """The text of path with each {literal: replacement} applied, every occurrence
    of it; a literal the file no longer has fails here, under its own name."""
    text = Path(path).read_text()
    for literal, replacement in rewrites.items():
        assert literal in text, f"{path} no longer contains {literal!r}"
        text = text.replace(literal, replacement)
    return text


def image_copies():
    """(sources, destination) of every build-context COPY in the AF Dockerfile."""
    copies = []
    for line in (AF_IMAGE / "Dockerfile").read_text().replace("\\\n", " ").splitlines():
        if line.startswith("COPY ") and "--from=" not in line:
            *sources, destination = [
                word for word in line.split()[1:] if not word.startswith("--")
            ]
            copies.append((sources, destination))
    return copies


def image_file(path):
    """The repo file the image installs at path, or None."""
    for sources, destination in image_copies():
        for source in sources:
            name = Path(source).name if destination.endswith("/") else ""
            if destination + name == path:
                return REPO / source
    return None


def startup_hooks():
    """The repo file of each installed hook, in the order run-hooks sources them."""
    hooks = [
        REPO / source
        for sources, destination in image_copies()
        if destination == HOOKS_IN_IMAGE
        for source in sources
    ]
    return sorted(hooks, key=lambda hook: hook.name)


def load_jupyter_config(monkeypatch, env=None):
    """Exec jupyter_server_config.py the way `jupyter server` does; return
    (globals, config sink).

    Every session variable the config reads is cleared unless a test sets it,
    so a developer's own shell cannot decide what the config under test
    produces."""
    for var in (
        "JUPYTERHUB_API_TOKEN",
        "NAMESPACE",
        "PYROSCOPE_SERVER",
        "PYROSCOPE_APP",
    ):
        monkeypatch.delenv(var, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sink = ConfigSink()
    ns = {"get_config": lambda: sink, "__name__": "jupyter_server_config"}
    code = JUPYTER_CONFIG.read_text()
    exec(compile(code, str(JUPYTER_CONFIG), "exec"), ns)
    return ns, sink
