# confluence-mcp

An [MCP](https://modelcontextprotocol.io/) server for Confluence Cloud. Lets LLMs search, browse, read, and edit Confluence pages.

Includes optional **semantic search** — pages are automatically indexed with vector embeddings as they're accessed, and bulk indexing can be triggered on-demand.

## Setup

### 1. Get an API token

Create a Confluence API token at https://id.atlassian.com/manage-profile/security/api-tokens

### 2. Install

```bash
pipx install git+https://github.com/jameseperry/confluence-mcp.git
```

Or for local development:

```bash
git clone https://github.com/jameseperry/confluence-mcp.git
cd confluence-mcp
pipx install -e .
```

### 3. Configure environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CONFLUENCE_BASE_URL` | Yes | Your Atlassian URL, e.g. `https://yourorg.atlassian.net` |
| `CONFLUENCE_EMAIL` | Yes | Your Atlassian account email |
| `CONFLUENCE_API_TOKEN` | Yes | API token from step 1 |
| `CONFLUENCE_MAX_LENGTH` | No | Max characters per response (default: 50000) |
| `EMBEDDING_API_URL` | No | URL for embedding API. Enables semantic search when set. |

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
| `get_page` | Fetch a page by ID. Returns Markdown (default) or XHTML. Large pages return an outline instead of full content — use `allow_large=true` to override. |
| `get_page_outline` | Get a page's heading structure as a table of contents. |
| `get_page_section` | Read a specific section by heading name. Supports Markdown or XHTML format. |
| `get_page_by_title` | Find a page by title within a space (exact match, then fuzzy fallback). Returns metadata only. |
| `list_spaces` | List available Confluence spaces. |
| `get_space_pages` | List top-level pages in a space. |
| `get_child_pages` | List child pages of a given page. |

### Writing

All write tools use Confluence storage format (XHTML) and handle versioning automatically.

| Tool | Description |
|------|-------------|
| `create_page` | Create a new page in a space with XHTML content. |
| `update_page` | Replace an entire page's content. Handles versioning automatically. |
| `update_page_section` | Replace the content under a specific heading. |
| `append_to_page` | Append content at the end of a page. |
| `append_to_section` | Append content at the end of a specific section. |
| `move_section` | Move a section to a new position on the page. Can also rename the heading or change its level. |
| `delete_section` | Delete a section. Can optionally preserve child subsections. |

### Comments, labels, and page management

| Tool | Description |
|------|-------------|
| `get_comments` | Get comments on a page (bodies returned as Markdown). |
| `add_comment` | Add a comment to a page (XHTML body). |
| `add_label` | Add a label to a page. |
| `delete_page` | Delete a page. |

### Semantic search

Requires `EMBEDDING_API_URL` to be configured.

| Tool | Description |
|------|-------------|
| `semantic_search` | Vector-based semantic search across indexed pages. Supports multi-perspective queries. |

### Index management

Tools for managing the semantic index. Pages are automatically indexed in the background as they're fetched through other tools. These tools give control over bulk indexing and perspectives.

| Tool | Description |
|------|-------------|
| `index_status` | Show per-scope indexed files, chunks, and stale counts. |
| `list_index_scopes` | List configured scopes (spaces or page trees for bulk indexing). |
| `add_index_scope` | Add a scope for bulk indexing (a whole space or a page tree). |
| `remove_index_scope` | Remove an index scope. |
| `index_now` | Trigger bulk indexing of a scope. Runs as a background task. |
| `list_perspectives` | List current embedding perspectives with names and instructions. |
| `add_perspective` | Add a new embedding perspective. Existing chunks are lazily re-embedded. |
| `remove_perspective` | Remove a perspective and drop its vector table. |

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

1. `get_page(page_id, format="xhtml")` to get the full XHTML
2. Edit the XHTML
3. `update_page(page_id, new_content)` to push the change

## Running standalone

```bash
# stdio (default, for MCP clients)
confluence-mcp

# HTTP server
confluence-mcp --transport http --host 127.0.0.1 --port 8767

# SSE server
confluence-mcp --transport sse --host 127.0.0.1 --port 8767
```

## Semantic search

Pages are automatically indexed in the background as they're accessed through MCP tools. For bulk indexing of entire spaces or page trees, use the index management tools or the CLI.

### Perspectives

Perspectives control how pages are embedded for search. Each perspective uses a different instruction to bias the embedding toward a particular concern. Default perspectives:

- **technical** — technical specifications, architecture, and implementation details
- **project** — schedules, milestones, project status, and deliverables

Add custom perspectives with `add_perspective`. Existing chunks are lazily re-embedded on next access.

### CLI

A separate `confluence-indexer` CLI is available for indexing operations outside the MCP server:

```bash
# Build the index (one-shot)
confluence-indexer index

# Index a specific scope
confluence-indexer index --scope dev_docs -v

# Start as API server (re-indexes on a schedule)
confluence-indexer serve

# CLI semantic search
confluence-indexer search "how does authentication work" --scope dev_docs

# Show index status
confluence-indexer status
```

### Additional configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_API_DIMENSIONS` | 768 | Embedding vector dimensions |
| `EMBEDDING_API_BATCH_SIZE` | 128 | Max texts per embedding API request |
| `EMBEDDING_API_MAX_CONCURRENT` | 4 | Max concurrent embedding requests |
| `EMBEDDING_MAX_CHUNK_CHARS` | 2000 | Max characters per chunk when indexing |
