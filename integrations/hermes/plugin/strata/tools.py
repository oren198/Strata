"""The tools the provider exposes to the agent, and the one place that names the write tools.

Every write goes through ``strata_contribute`` (which the Strata judge reviews); the provider's
lifecycle hooks never reach this module. ``strata_status`` is answered locally and is read-only:
the scope comes from the profile's environment, never from the agent.
"""

from __future__ import annotations

from typing import Any

READ_PERSPECTIVE = "strata_read_perspective"
STATUS = "strata_status"

_CLASSIFICATIONS = ["directive", "context"]

SYSTEM_BLOCK = (
    "Shared memory: this agent is bound to the Strata scope {scope}. The memory recalled for "
    "each turn is what the fleet has agreed. To add to it, call strata_contribute; a judge "
    "reviews every contribution and may decline it."
)

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": READ_PERSPECTIVE,
        "description": (
            "Read what this fleet's shared Strata memory says for your scope: the directives "
            "that bind you and your scope's context. Read it before you act."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "strata_contribute",
        "description": (
            "Propose something for the shared Strata memory: a decision, finding or gap. A judge "
            "reviews it before it counts, and may decline it. Use classification 'directive' for "
            "a rule that should bind, 'context' for a fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to record."},
                "proposed_classification": {"type": "string", "enum": _CLASSIFICATIONS},
                "subject": {"type": "string", "description": "A short subject tag."},
                "supersedes": {"type": "string", "description": "The id of a directive replaced."},
                "acted_on": {"type": "string", "description": "The id of a memory item acted on."},
            },
            "required": ["content", "proposed_classification"],
        },
    },
    {
        "name": "strata_session_closeout",
        "description": "Say that nothing from this session is worth recording, with the reason.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "strata_rejudge",
        "description": "Ask the judge to look again at a contribution it has not judged yet.",
        "parameters": {
            "type": "object",
            "properties": {"contribution_id": {"type": "string"}},
            "required": ["contribution_id"],
        },
    },
    {
        "name": STATUS,
        "description": (
            "Show the Strata scope and skill this profile is bound to, and whether the "
            "server is up."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

_FORWARDED = {
    "strata_contribute": (
        "content",
        "proposed_classification",
        "subject",
        "supersedes",
        "acted_on",
    ),
    "strata_session_closeout": ("reason",),
    "strata_rejudge": ("contribution_id",),
}


def server_arguments(tool_name: str, args: dict[str, Any], scope: str) -> dict[str, Any]:
    """The arguments sent to the Strata tool: only the declared ones, plus the bound scope for
    the tools that name one. The agent can never choose the scope."""
    if tool_name not in _FORWARDED:
        raise KeyError(tool_name)
    sent = {key: args[key] for key in _FORWARDED[tool_name] if args.get(key) is not None}
    if tool_name == "strata_contribute":
        sent["scope_id"] = scope
    return sent
