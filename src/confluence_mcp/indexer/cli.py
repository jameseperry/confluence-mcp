"""CLI entry point for the Confluence indexer (index, search, status)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def _init_indexer():
    """Initialize embedding and DB from env vars. Returns (conn, embed_config) or exits."""
    from confluence_mcp.config import (
        get_db_path,
        get_default_perspectives,
        get_embedding_config,
    )

    from .db import init_db, sync_perspectives
    from .embeddings import init_embedder

    embed_config = get_embedding_config()
    if embed_config is None:
        print("Error: EMBEDDING_API_URL not set", file=sys.stderr)
        sys.exit(1)

    init_embedder(embed_config)
    perspectives = get_default_perspectives()
    db_path = get_db_path()
    conn = init_db(db_path, perspectives, embed_config.dimensions)
    sync_perspectives(conn, perspectives, embed_config.dimensions)
    return conn, embed_config


def cmd_index(args: argparse.Namespace) -> None:
    from confluence_mcp.client import ConfluenceClient
    from confluence_mcp.config import get_api_token, get_base_url, get_email, get_max_chunk_chars

    from .db import close_all_dbs, list_index_scopes, add_index_scope
    from .pipeline import index_scope

    conn, embed_config = _init_indexer()

    # Determine scopes to index
    if args.space:
        # Ad-hoc space indexing — add to DB if not present, then index
        scopes = [{"label": args.space, "scope_type": "space", "scope_id": args.space}]
        existing = list_index_scopes(conn)
        if not any(s["label"] == args.space for s in existing):
            add_index_scope(conn, args.space, "space", args.space)
            print(f"Added scope '{args.space}' to index database")
    elif args.page_tree:
        scopes = [{"label": f"tree-{args.page_tree}", "scope_type": "page_tree", "scope_id": args.page_tree}]
        existing = list_index_scopes(conn)
        label = scopes[0]["label"]
        if not any(s["label"] == label for s in existing):
            add_index_scope(conn, label, "page_tree", args.page_tree)
            print(f"Added scope '{label}' to index database")
    else:
        scopes = list_index_scopes(conn)
        if not scopes:
            print("Error: no scopes configured. Use --space or --page-tree, or add via MCP tools.", file=sys.stderr)
            sys.exit(1)

    async def _run() -> None:
        async with ConfluenceClient(
            get_base_url(), get_email(), get_api_token()
        ) as client:
            for scope in scopes:
                print(f"\nIndexing '{scope['label']}' ({scope['scope_type']}: {scope['scope_id']})...")
                stats = await index_scope(
                    client, conn,
                    scope_type=scope["scope_type"],
                    scope_id=scope["scope_id"],
                    label=scope["label"],
                    max_chunk_chars=get_max_chunk_chars(),
                    force=args.full,
                )
                print(f"  Indexed: {stats['indexed']}")
                print(f"  Updated: {stats['updated']}")
                print(f"  Unchanged: {stats['unchanged']}")
                print(f"  Removed: {stats['removed']}")
                print(f"  Chunks: {stats['chunks']}")
                if stats["errors"]:
                    print(f"  Errors: {stats['errors']}")

    asyncio.run(_run())
    close_all_dbs()


def cmd_search(args: argparse.Namespace) -> None:
    from .db import close_all_dbs
    from .search import semantic_search

    conn, _ = _init_indexer()

    results = semantic_search(
        conn,
        args.query,
        perspective=args.perspective or None,
        limit=args.limit,
    )

    for r in results:
        staleness = r.get("staleness", "unknown")
        interval = r.get("median_update_interval", "unknown")
        print(
            f"\n--- {r['score']:.3f} | {r['title']} | "
            f"{r['heading'] or '(root)'} | {staleness} (updates {interval})"
        )
        print(f"    Page ID: {r['page_id']}")
        if r.get("last_accessed"):
            print(f"    Last seen: {r['last_accessed']}")
        print(f"    {r['snippet'][:200]}...")

    close_all_dbs()


def cmd_status(args: argparse.Namespace) -> None:
    from confluence_mcp.config import get_db_path

    from .db import close_all_dbs, get_perspectives, list_index_scopes

    conn, _ = _init_indexer()

    page_count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    last = conn.execute("SELECT MAX(last_indexed) FROM pages").fetchone()[0]
    persp = get_perspectives(conn)
    scopes = list_index_scopes(conn)

    print(f"\nIndex: {get_db_path()}")
    print(f"  Pages: {page_count}")
    print(f"  Chunks: {chunk_count}")
    print(f"  Last indexed: {last or 'never'}")
    print(f"  Perspectives: {', '.join(p['name'] for p in persp)}")
    if scopes:
        print(f"  Scopes:")
        for s in scopes:
            print(f"    - {s['label']} ({s['scope_type']}: {s['scope_id']})")
    else:
        print(f"  Scopes: (none configured)")

    close_all_dbs()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="confluence-indexer",
        description="Confluence semantic indexer",
    )

    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="Run a one-shot index pass")
    p_index.add_argument("--space", default="", help="Index a space by key")
    p_index.add_argument("--page-tree", default="", help="Index a page tree by root page ID")
    p_index.add_argument("--full", action="store_true", help="Force full re-index")
    p_index.add_argument("-v", "--verbose", action="store_true")

    p_search = sub.add_parser("search", help="Run a CLI semantic search")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--perspective", default="")
    p_search.add_argument("-n", "--limit", type=int, default=10)
    p_search.add_argument("-v", "--verbose", action="store_true")

    p_status = sub.add_parser("status", help="Show index status")

    args = parser.parse_args()

    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "index":
        cmd_index(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
