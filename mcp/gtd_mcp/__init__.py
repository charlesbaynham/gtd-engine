"""gtd_mcp — an MCP server exposing the GTD vault as structured tools.

Imports gtd_ci for all parsing, rendering and job logic; this package adds
only handle addressing, semantic write operations, read views and the
git-backed store around them. See README.md for how to run it.
"""
from __future__ import annotations

__version__ = "0.1.0"
