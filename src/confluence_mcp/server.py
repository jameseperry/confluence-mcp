"""CLI entry point for the Confluence MCP server."""

from __future__ import annotations

import argparse

from .app import create_mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Confluence MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()

    mcp = create_mcp_server()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
