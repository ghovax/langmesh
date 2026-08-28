Call a tool that a configured MCP server offers.

- Find the exact `tool_name` and the schema for `arguments` with `list_mcp_tools` first.
- Treat safety as in `bash`: `access_request` with `mutates: false` to inspect, `mutates: true` to change.
- An MCP server runs outside this machine's confinement: a call that changes anything is put to whoever decides for this session unless a rule already names it.

Arguments:

- `server` — The name of a configured MCP server.
- `tool_name` — The tool name, as `list_mcp_tools` reports it.
- `arguments` — A JSON object that matches the MCP server tool's input schema.
