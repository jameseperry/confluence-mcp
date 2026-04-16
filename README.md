# confluence-mcp

A read-only [MCP](https://modelcontextprotocol.io/) server for Confluence Cloud. Lets LLMs search, browse, and read Confluence pages as Markdown.

## Setup

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Get an API token

Create a Confluence API token at https://id.atlassian.com/manage-profile/security/api-tokens

### 3. Configure environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CONFLUENCE_BASE_URL` | Yes | Your Atlassian URL, e.g. `https://yourorg.atlassian.net` |
| `CONFLUENCE_EMAIL` | Yes | Your Atlassian account email |
| `CONFLUENCE_API_TOKEN` | Yes | API token from step 2 |
| `CONFLUENCE_MAX_LENGTH` | No | Max characters per response (default: 50000) |

### 4. Add to Claude Code

Add to your Claude Code MCP config (`.claude/settings.json` or similar):

```json
{
  "mcpServers": {
    "confluence": {
      "command": "/path/to/confluence-mcp/.venv/bin/confluence-mcp",
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

| Tool | Description |
|------|-------------|
| `search` | Search pages by text or raw CQL query. Plain text is auto-wrapped; CQL like `space.key = "DEV" AND label = "api"` is passed through directly. |
| `get_page` | Fetch a page by ID and return its content as Markdown. Long pages are paginated with `start_offset`. |
| `get_page_outline` | Get a page's heading structure as a table of contents. |
| `get_page_section` | Read a specific section by heading name, with optional sub-section inclusion and pagination. |
| `get_page_by_title` | Find a page by title within a space (exact match, then fuzzy fallback). Returns metadata only. |
| `list_spaces` | List available Confluence spaces. |
| `get_space_pages` | List top-level pages in a space. |
| `get_child_pages` | List child pages of a given page. |

## Typical workflow

1. `list_spaces` to discover available spaces
2. `search` or `get_page_by_title` to find a page
3. `get_page_outline` to see the heading structure
4. `get_page_section` to read just the section you need
5. `get_page` for the full content, using `start_offset` to paginate if truncated

## Running standalone

```bash
# stdio (default, for MCP clients)
confluence-mcp

# HTTP server
confluence-mcp --transport http --host 127.0.0.1 --port 8767

# SSE server
confluence-mcp --transport sse --host 127.0.0.1 --port 8767
```
