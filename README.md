# confluence-mcp

An [MCP](https://modelcontextprotocol.io/) server for Confluence Cloud. Lets LLMs search, browse, read, and edit Confluence pages.

Includes an optional **semantic indexer** service that maintains vector embeddings of Confluence pages for similarity search.

## Setup

### 1. Get an API token

Create a Confluence API token at https://id.atlassian.com/manage-profile/security/api-tokens

### 2. Install

#### MCP server only

```bash
pipx install git+https://github.com/jameseperry/confluence-mcp.git
```

Or for local development:

```bash
git clone https://github.com/jameseperry/confluence-mcp.git
cd confluence-mcp
pipx install -e .
```

#### MCP server + semantic indexer

```bash
pipx install "git+https://github.com/jameseperry/confluence-mcp.git[indexer]"
```

Or for local development:

```bash
pipx install -e '.[indexer]'
```

This pulls in additional dependencies: FastAPI, uvicorn, sentence-transformers, sqlite-vec, and numpy.

### 3. Configure environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CONFLUENCE_BASE_URL` | Yes | Your Atlassian URL, e.g. `https://yourorg.atlassian.net` |
| `CONFLUENCE_EMAIL` | Yes | Your Atlassian account email |
| `CONFLUENCE_API_TOKEN` | Yes | API token from step 1 |
| `CONFLUENCE_MAX_LENGTH` | No | Max characters per response (default: 50000) |

### 4. Add to Claude Code

Add to your Claude Code MCP config (`.claude/settings.json` or similar):

```json
{
  "mcpServers": {
    "confluence": {
      "command": "confluence-mcp",
      "env": {
        "CONFLUENCE_BASE_URL": "https://yourorg.atlassian.net",
        "CONFLUENCE_EMAIL": "you@example.com",
        "CONFLUENCE_API_TOKEN": "your-api-token"
      }
    }
  }
}
```

## Tools

### Reading

| Tool | Description |
|------|-------------|
| `search` | Search pages by text or raw CQL query. Plain text is auto-wrapped; CQL like `space.key = "DEV" AND label = "api"` is passed through directly. |
| `get_page` | Fetch a page by ID. Use `format='md'` (default) for Markdown or `format='xhtml'` for raw storage format. Long pages are paginated with `start_offset`. |
| `get_page_outline` | Get a page's heading structure as a table of contents. |
| `get_page_section` | Read a specific section by heading name. Supports `format='md'` or `format='xhtml'`. |
| `get_page_by_title` | Find a page by title within a space (exact match, then fuzzy fallback). Returns metadata only. |
| `list_spaces` | List available Confluence spaces. |
| `get_space_pages` | List top-level pages in a space. |
| `get_child_pages` | List child pages of a given page. |

### Writing

All write tools use Confluence storage format (XHTML).

| Tool | Description |
|------|-------------|
| `create_page` | Create a new page in a space with XHTML content. |
| `update_page` | Replace an entire page's content. Requires explicit version number for optimistic locking. |
| `update_page_section` | Replace the content under a specific heading. Handles versioning automatically. |
| `append_to_page` | Append content at the end of a page. No need to read first. |
| `append_to_section` | Append content at the end of a specific section. |

### Comments, labels, and page management

| Tool | Description |
|------|-------------|
| `get_comments` | Get comments on a page (bodies returned as Markdown). |
| `add_comment` | Add a comment to a page (XHTML body). |
| `add_label` | Add a label to a page. |
| `delete_page` | Delete a page. |

## Typical workflows

### Reading

1. `list_spaces` to discover available spaces
2. `search` or `get_page_by_title` to find a page
3. `get_page_outline` to see the heading structure
4. `get_page_section` to read just the section you need
5. `get_page` for the full content, using `start_offset` to paginate if truncated

### Editing a section

1. `get_page_section(page_id, "Section Name", format="xhtml")` to get the section's XHTML
2. Modify the XHTML content
3. `update_page_section(page_id, "Section Name", new_content)` to push the change

### Full page editing

1. `get_page(page_id, format="xhtml")` to get the full XHTML and version number
2. Edit the XHTML
3. `update_page(page_id, title, new_content, version + 1)` to push the change

## Running standalone

```bash
# stdio (default, for MCP clients)
confluence-mcp

# HTTP server
confluence-mcp --transport http --host 127.0.0.1 --port 8767

# SSE server
confluence-mcp --transport sse --host 127.0.0.1 --port 8767
```

## Semantic indexer

The semantic indexer is a standalone FastAPI service that maintains vector embeddings of Confluence pages for similarity search. It indexes pages from defined scopes (an entire space or descendants of a specific page) and supports multi-perspective semantic queries.

### Configuration

Create an `indexer_config.json`:

```json
{
  "confluence": {
    "base_url": "https://yourorg.atlassian.net",
    "email": "you@example.com",
    "api_token": "your-api-token"
  },
  "scopes": [
    {
      "label": "dev_docs",
      "scope_type": "space",
      "scope_id": "DEV"
    },
    {
      "label": "arch_docs",
      "scope_type": "page_tree",
      "scope_id": "123456789",
      "perspectives": [
        {"name": "general", "instruction": "general knowledge and concepts"},
        {"name": "technical", "instruction": "technical specifications and implementation details"},
        {"name": "onboarding", "instruction": "onboarding procedures and getting started guides"}
      ]
    }
  ],
  "embedding": {
    "backend": "local",
    "model_name": "nomic-ai/nomic-embed-text-v1.5",
    "dimensions": 768
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8400,
    "data_dir": "data",
    "reindex_interval_hours": 24
  }
}
```

Each scope gets its own SQLite database at `{data_dir}/{label}.db` (e.g. `data/dev_docs.db`). You can override this per scope with a `db_path` field. Perspectives default to general, technical, and procedural but can be customized per scope.

#### Additional options

| Field | Default | Description |
|-------|---------|-------------|
| `embedding.api_url` | `""` | URL for a remote embedding API (when `backend` is `"remote"`) |
| `embedding.batch_size` | `128` | Max texts per embedding API request |
| `embedding.max_concurrent` | `4` | Max concurrent embedding API requests |
| `server.token_ttl_hours` | `24` | How long an auth token remains valid |
| `max_chunk_chars` | `2000` | Max characters per chunk when splitting pages for indexing |

### Usage

```bash
# Build the index (one-shot, no server)
confluence-indexer index

# Index a specific scope
confluence-indexer index --scope dev_docs -v

# Start the API server (re-indexes automatically on a schedule)
confluence-indexer serve

# CLI semantic search
confluence-indexer search "how does authentication work" --scope dev_docs

# Show index status
confluence-indexer status
```

### API endpoints

The indexer API uses challenge-response authentication to verify callers have Confluence access.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check |
| GET | `/auth/challenge` | No | Get an auth challenge (returns token + page to verify against) |
| PUT | `/auth/verify` | No | Submit challenge response to verify token |
| POST | `/search` | Yes | Semantic search across indexed pages |
| POST | `/reindex` | Yes | Trigger re-indexing (returns 202) |
| GET | `/status` | Yes | Per-scope index stats |

Authenticated endpoints require an `Authorization: Bearer <token>` header.
