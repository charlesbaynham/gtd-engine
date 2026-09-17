"""reMarkable round-trip for the GTD vault.

`tasks.py` turns the vault into the tasks JSON `remarkable-gtd` renders;
`apply.py` turns the decisions JSON its scanner produces back into vault
operations (via `gtd_mcp.ops`, so FORMAT.md is honoured by construction);
`cli.py` strings download → scan → apply → publish together for CI.
"""
