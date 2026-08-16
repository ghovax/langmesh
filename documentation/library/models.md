# Models, credentials, and cache behavior

## Select a provider model

Set `provider` and `model` on `AgentConfiguration` when LangMesh should build the provider adapter. `model_identifier="provider/model"` on `Session` overrides that profile for one run.

```python
agent = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4-5",
    system_prompt="Review changes and cite evidence.",
)

session = Session(
    agent,
    directory="/srv/checkout",
    model_identifier="openai/gpt-5.2",
    providers={"openai": "sk-…"},
)
```

The `providers` mapping is copied into the session's `Configuration`; the caller's value is never mutated. Environment variables still take precedence. Account-backed providers use the replaceable `Credentials` port in `SessionComponents`.

## Supply a model object

Pass any LangChain `BaseChatModel` through `SessionComponents.model` when the application owns provider construction, routing, retries, or testing.

```python
components = SessionComponents(model=application_model)
session = Session(agent_without_provider, directory="/srv/checkout", components=components)
```

The model must implement `bind_tools()` and streaming. LangMesh binds one stable ordered tool schema when the runtime is constructed.

## Preserve provider caches

The static system prompt and tool schema form the reusable prefix. LangMesh preserves that prefix by construction:

- `RuntimeComponents` is frozen and snapshots sequence fields.
- Prior conversation messages are append-only until an explicit compaction.
- Permission and goal reviewers inherit the main conversation and stable tool schema, then append their private instructions.
- A tool granted to a session is described by an appended conversation message, not a schema change, so the prefix holds at any moment. See [Granting a tool to a session](customization.md#granting-a-tool-to-a-session).
- Steering appends at a provider boundary; it never edits an earlier message.
- Permission-mode and location changes apply during execution without rewriting model history.

`PromptComposer` runs only when the cached system prompt is built. Call `Session.refresh_prompt()` after changing an external source that the composer reads; that explicit refresh invalidates the static prompt cache. A `BeforeModelHook` runs on every request and can intentionally change the prefix, so cache-sensitive hooks should leave the first system message untouched.

Usage events expose provider-reported cache reads, the reachable prefix, and the first divergence from the preceding request. Use these values to verify a custom model adapter instead of inferring cache behavior from latency alone.
