Stop one other live tool call in this session by the identifier that call was given.

- Use this when a still-running call should not finish: a backgrounded command, a search or fetch that has not returned, a screen-control script, or any other in-flight call whose id you have.
- The identifier is the tool-call id on that call, not a background job handle (`bg-…`, `search-…`) and not a turn id.
- You cannot stop the `stop_tool_call` you are making.

Arguments:
- `tool_call_id` — The id of the live tool call to terminate.
