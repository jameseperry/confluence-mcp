"""CLI entry point for the Confluence semantic indexer."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from confluence_indexer.config import get_settings, init_settings

    init_settings(args.config)
    settings = get_settings()

    host = args.host or settings.server.host
    port = args.port or settings.server.port

    uvicorn.run(
        "confluence_indexer.api:app",
        host=host,
        port=port,
        log_level="info",
    )


def cmd_index(args: argparse.Namespace) -> None:
    from confluence_indexer.config import get_settings, init_settings
    from confluence_indexer.db import close_all_dbs, init_db, sync_perspectives
    from confluence_indexer.indexer import IndexerClient, index_scope

    init_settings(args.config)
    settings = get_settings()

    scopes = settings.scopes
    if args.scope:
        scopes = [s for s in scopes if s.label == args.scope]
        if not scopes:
            print(f"Error: scope '{args.scope}' not found in config", file=sys.stderr)
            sys.exit(1)

    for scope in scopes:
        conn = init_db(scope.label, scope.db_path, scope.perspectives)
        sync_perspectives(conn, scope.perspectives)

    async def _run() -> None:
        async with IndexerClient(
            settings.confluence.base_url,
            settings.confluence.email,
            settings.confluence.api_token,
        ) as client:
            for scope in scopes:
                from confluence_indexer.db import get_conn

                print(f"\nIndexing scope '{scope.label}' ({scope.scope_type}: {scope.scope_id})...")
                conn = get_conn(scope.label)
                stats = await index_scope(client, scope, conn, force=args.full)
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
    from confluence_indexer.config import get_settings, init_settings
    from confluence_indexer.db import close_all_dbs, get_conn, get_perspectives, init_db
    from confluence_indexer.embeddings import get_embedder, serialize_vector

    init_settings(args.config)
    settings = get_settings()

    scopes = settings.scopes
    if args.scope:
        scopes = [s for s in scopes if s.label == args.scope]
        if not scopes:
            print(f"Error: scope '{args.scope}' not found in config", file=sys.stderr)
            sys.exit(1)

    for scope in scopes:
        init_db(scope.label, scope.db_path, scope.perspectives)

    results: list[tuple[float, str, str, str, str, str]] = []

    for scope in scopes:
        conn = get_conn(scope.label)
        perspectives = get_perspectives(conn)

        for p in perspectives:
            query_vec = get_embedder().embed_query(args.query, p["instruction"])
            vec_bytes = serialize_vector(query_vec)
            table = f"vec_p{p['id']}"

            try:
                rows = conn.execute(
                    f"SELECT rowid, distance FROM [{table}] "
                    f"WHERE embedding MATCH ? AND k = ?",
                    (vec_bytes, args.limit * 2),
                ).fetchall()
            except Exception as e:
                logger.error("Vec search failed on %s: %s", table, e)
                continue

            for row in rows:
                chunk = conn.execute(
                    "SELECT page_db_id, heading_path, content FROM chunks WHERE id = ?",
                    (row["rowid"],),
                ).fetchone()
                if not chunk:
                    continue
                page = conn.execute(
                    "SELECT page_id, title FROM pages WHERE id = ?",
                    (chunk["page_db_id"],),
                ).fetchone()
                if not page:
                    continue

                score = 1.0 - row["distance"]
                results.append((
                    score,
                    page["page_id"],
                    page["title"],
                    chunk["heading_path"],
                    chunk["content"][:200],
                    scope.label,
                ))

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[float, str, str, str, str, str]] = []
    for r in sorted(results, key=lambda x: -x[0]):
        key = (r[1], r[3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    for score, page_id, title, heading, snippet, scope_label in unique[: args.limit]:
        print(f"\n--- {score:.3f} | {title} | {heading or '(root)'} [{scope_label}]")
        print(f"    Page ID: {page_id}")
        print(f"    {snippet}...")

    close_all_dbs()


def cmd_status(args: argparse.Namespace) -> None:
    from confluence_indexer.config import get_settings, init_settings
    from confluence_indexer.db import close_all_dbs, get_conn, get_perspectives, init_db

    init_settings(args.config)
    settings = get_settings()

    scopes = settings.scopes
    if args.scope:
        scopes = [s for s in scopes if s.label == args.scope]

    for scope in scopes:
        init_db(scope.label, scope.db_path, scope.perspectives)

    for scope in scopes:
        conn = get_conn(scope.label)
        page_count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        last = conn.execute("SELECT MAX(last_indexed) FROM pages").fetchone()[0]
        persp = get_perspectives(conn)

        print(f"\nScope: {scope.label} ({scope.scope_type}: {scope.scope_id})")
        print(f"  Pages: {page_count}")
        print(f"  Chunks: {chunk_count}")
        print(f"  Last indexed: {last or 'never'}")
        print(f"  Perspectives: {', '.join(p['name'] for p in persp)}")
        print(f"  DB: {scope.db_path}")

    close_all_dbs()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="confluence-indexer",
        description="Confluence semantic indexer",
    )
    parser.add_argument(
        "--config", default="indexer_config.json", help="Path to config file"
    )

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="")
    p_serve.add_argument("--port", type=int, default=0)

    p_index = sub.add_parser("index", help="Run a one-shot index pass")
    p_index.add_argument("--scope", default="", help="Index only this scope")
    p_index.add_argument("--full", action="store_true", help="Force full re-index")
    p_index.add_argument("-v", "--verbose", action="store_true")

    p_search = sub.add_parser("search", help="Run a CLI semantic search")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--scope", default="")
    p_search.add_argument("-n", "--limit", type=int, default=10)
    p_search.add_argument("-v", "--verbose", action="store_true")

    p_status = sub.add_parser("status", help="Show index status")
    p_status.add_argument("--scope", default="")

    args = parser.parse_args()

    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "index":
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
