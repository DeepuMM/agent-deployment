# Microsoft Learn docs (tool)

Gives agents in **models-demo** search and read access to Microsoft Learn documentation through Microsoft's public
MCP server (`https://learn.microsoft.com/api/mcp`), fronted by the AI gateway.

| MCP tool | Impact | What it does |
|---|---|---|
| `microsoft_docs_search` | read | Searches Microsoft Learn and returns relevant excerpts with their URLs |
| `microsoft_docs_fetch` | read | Returns a Microsoft Learn page as markdown |

`microsoft_code_sample_search` is also on the server but is not exposed: the gateway refuses calls to it.

**Use in an agent** (`agent.yaml`):

```yaml
tools:
  - tool: ms-learn
    allowed_tools: [microsoft_docs_search]
```

**Owner:** ai-platform-team. The server is public and needs no credentials, so there is no backend access to grant.
