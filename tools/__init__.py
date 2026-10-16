"""Narrow, reusable operations the agents and the pipeline call: one subpackage per domain.

`tools.data` (cache-first clients for external biomedical services), `tools.experiment` (the registered
experiment boundary), `tools.graph`, `tools.literature` (the literature store and its MCP server) and
`tools.paper`. Grouping them says what they are once, instead of five sibling top-level packages, and keeps
the repository root for the project's main directories.

Nothing is re-exported here: a caller imports the subpackage it needs, so this file stays a namespace and not
a second place where the tool surface is declared.
"""
