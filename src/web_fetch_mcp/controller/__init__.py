"""Controller layer: the FastMCP boundary.

Defines the MCP server, the thin ``fetch``/``screenshot`` tools (which delegate to
the service layer), lifespan-managed browser teardown, and the ``main`` entry
point. This is the only layer aware of the MCP framework.
"""
