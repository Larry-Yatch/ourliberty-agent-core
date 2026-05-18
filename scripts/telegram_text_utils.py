"""Telegram text-handling helpers shared by the agent bots."""


def strip_leading_slash(text: str) -> str:
    """Strip a single leading `/` so agent-level slash commands reach the agent.

    Claude Code's CLI parser intercepts a leading `/` as a built-in command
    (e.g. `/optimize` returns "Unknown command"). The agent's CLAUDE.md
    defines its own slash commands; this strip lets them through.

    `//` is reserved as an escape: a user who genuinely wants a literal
    leading slash forwarded to the agent can prefix with `//`.
    """
    if text.startswith("/") and not text.startswith("//"):
        return text[1:]
    return text
