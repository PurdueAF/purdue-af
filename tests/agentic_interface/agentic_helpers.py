"""Test helpers for the agentic-interface suite (sys.path set by conftest)."""

USER = {
    "username": "alice",
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


async def failure(awaitable):
    """Await a tool call that must fail; return the failure's message.

    Tools raise errors.Failure (an MCP ToolError) instead of returning error
    strings, so tests that assert on a failure message go through here.
    """
    from errors import Failure

    try:
        result = await awaitable
    except Failure as exc:
        return str(exc)
    raise AssertionError(f"tool call succeeded instead of failing: {result!r}")


async def needs_choices(awaitable):
    """Await a tool call that must ask for choices; return the help text.

    Tools raise NeedsChoices when a choice could not be elicited; the server
    turns it into an ordinary result, so tests that call tools directly go
    through here.
    """
    from tools.elicitation import NeedsChoices

    try:
        result = await awaitable
    except NeedsChoices as exc:
        return str(exc)
    raise AssertionError(f"tool call did not ask for choices: {result!r}")
