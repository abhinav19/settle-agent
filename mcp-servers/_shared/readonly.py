"""Common server construction: every tool is declared read-only and idempotent."""

import mcp_types

READ_ONLY = mcp_types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
