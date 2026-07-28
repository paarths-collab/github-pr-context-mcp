"""Application package with a lazy MCP server export."""

__all__ = ["mcp"]


def __getattr__(name: str):
    if name == "mcp":
        from app.mcp_app import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
