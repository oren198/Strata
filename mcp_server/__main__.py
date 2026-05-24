"""Entry point: ``python -m mcp_server.strata_mcp``."""

from __future__ import annotations

from mcp_server.strata_mcp import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
