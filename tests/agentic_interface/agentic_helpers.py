"""Test helpers for the agentic-interface suite (sys.path set by conftest)."""

USER = {
    "username": "alice",
    "pod_name": "purdue-af-alice",
    "namespace": "cms",
    "token": "tok-alice",
}


class ToolRecorder:
    """Stand-in for FastMCP that records registered tools and prompts."""

    def __init__(self):
        self.tools = {}
        self.prompts = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def prompt(self):
        def decorator(fn):
            self.prompts[fn.__name__] = fn
            return fn

        return decorator


def register_tools(module):
    """Run a tool module's register() and return its captured tools/prompts."""
    recorder = ToolRecorder()
    module.register(recorder)
    return recorder
