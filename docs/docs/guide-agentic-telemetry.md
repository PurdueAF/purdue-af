# What the facility records about AI agent use

The Analysis Facility ships several AI coding agents — Claude Code, Codex and
opencode in the terminal and in the VS Code interface, their personas in the
JupyterLab chat, and the [agentic interface](guide-agentic-interface.md) MCP
server that lets any agent drive your session. This page says exactly what is
recorded when you use them, and what is not.

The short version: **the facility records that you used an agent and what it
reached for — never what you or the model said.**

## What is recorded

| | |
|---|---|
| **Sessions** | that an agent started, which agent, its version, how long it ran, and how it exited |
| **Interface** | whether it ran in a terminal, the VS Code interface, or a programmatic driver such as the JupyterLab chat |
| **Volume** | token counts, the agent's own estimate of the cost, and how many lines of code it added or removed |
| **Tools** | the *names* of the tools an agent invoked, whether each succeeded, and whether you accepted or rejected its proposed edits |
| **MCP servers** | the names of the MCP servers an agent connected to, including ones you configured yourself, and whether the connection worked |
| **AF tool calls** | every call to the facility's own MCP server: your username, the tool, the outcome, which harness made it, and whether it came from inside a session or from your laptop |

All of it is attributed to your AF username, the same one every other
per-user metric in the facility uses, and all of it is kept for 30 days.

## What is not recorded

- **Your prompts.** Never — not the text, not a summary. The switch that
  would enable it is off in policy the facility applies to every session, and
  the attribute is dropped again at the collector, so it cannot be turned back
  on from inside a session.
- **Model responses.** Same two guarantees.
- **Shell commands, file contents and tool inputs.** The one exception is
  tool *parameters for MCP tools*, which are kept because that is where the
  MCP server and tool names live. Parameters for `Bash`, `Edit`, `Write` and
  `Read` are dropped before anything is stored.
- **Your code.** Nothing reads, indexes or ships the contents of your home or
  work directories.
- **Your model account.** The email address and account id your agent reports
  for its Anthropic or OpenAI login are discarded on arrival. The facility
  ships no model credentials and has no visibility into your account.

## Why

Three reasons, in order of how much they drive the work:

1. **Capacity planning.** Agents change what a session does — more short
   bursts of tool use, different memory profiles. We cannot size the facility
   for that without knowing how much of it there is.
2. **Knowing what to fix.** A tool on the AF MCP server that fails half the
   time is invisible unless failures are counted. Most of the agentic
   interface's rough edges have been found this way.
3. **Reporting.** The facility is funded on the basis of what it is used for,
   and agent use is now a real part of that.

## Where it goes

Nowhere outside the facility. Metrics land in the AF Prometheus and events in
the AF Loki, both in-cluster, and are visible on an admin-only Grafana
dashboard. Nothing is forwarded to Anthropic, OpenAI, or any other third
party — although note that using an agent at all sends your prompts to
whichever model provider you logged in to, which is between you and them.

## Turning it off

Telemetry is on by default and the facility asks you to leave it on: the
numbers are what justify keeping these tools available. It is not a lock,
though. Setting `OTEL_METRICS_EXPORTER=none` and `OTEL_LOGS_EXPORTER=none` in
your shell stops Claude Code and Codex reporting, and running an agent by its
full path in `/opt/npm-global/bin` skips the wrapper that records launches.

What you cannot switch off is the AF MCP server's own record of the calls your
agent makes to it. That is a server-side log of actions taken against
facility resources — starting sessions, scaling clusters — and it is kept for
the same reason every other privileged API keeps one.

If something here does not sit right with you, please
[get in touch](support.md) — this policy is meant to be arguable.
