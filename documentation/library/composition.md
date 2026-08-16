# Runtime composition

LangMesh separates three concerns:

| Value | Owns | Changes while running? |
| --- | --- | --- |
| `RuntimeProfile` | Agent, global configuration, identity, directories, confinement, locations, parent, reviewer capability | No |
| `RuntimeComponents` | Model and replaceable runtime capabilities | No; replace the value before construction |
| `SessionComponents` | All runtime components plus checkpoints, credentials, workspace management, and tracing | No; `Session` owns their lifetime |

The daemon uses the same `RuntimeProfile` and `RuntimeComponents` API as an embedder. Product persistence connects through `GoalReviewJournal`; the core never imports daemon or worker state.

## Direct runtime construction

Use `Session` unless your application already owns checkpointing, resource leases, and turn serialization. Direct construction suits a scheduler or another session host.

```python
from langmesh import AgentRuntime, RuntimeComponents, RuntimeProfile

profile = RuntimeProfile(
    agent=agent,
    configuration=configuration,
    session_id="session-018f",
    working_directory="/srv/checkout",
    permission_mode="ask",
    sandbox=sandbox_profile,
)
components = RuntimeComponents(
    model=model,
    catalogue=catalogue,
    jobs=job_store,
    transcript=transcript,
    permissions=permission_policy,
    file_leases=file_leases,
)
runtime = AgentRuntime(profile, components)

async for event in runtime.stream("Inspect the current change."):
    consume(event)
```

`RuntimeProfile` requires a non-empty session id and an absolute working directory. `RuntimeComponents` validates structural ports at construction and copies mutable sequences to tuples.

## Session composition

`SessionComponents` extends `RuntimeComponents` with ownership seams.

```python
from langmesh import Session, SessionComponents

components = SessionComponents(
    model=model,
    catalogue=catalogue,
    checkpoints=checkpoints,
    jobs=job_store,
    transcript=transcript,
    credentials=credentials,
    workspace=workspace_manager,
    tracer_provider=tracer_provider,
)
session = Session(
    agent,
    directory="/srv/checkout",
    configuration=configuration,
    components=components,
)
```

The constructor keeps run facts (directory, identity, permission mode, confinement, model identifier, locations) outside the component value, so a persistence adapter cannot silently change confinement or identity.

## Component reference

| Component | Port or adopted interface | Default |
| --- | --- | --- |
| `model` | LangChain `BaseChatModel` | Built from the agent and configuration |
| `catalogue` | `CatalogueLike` | Project plus packaged catalogue; no home-directory lookup |
| `jobs` | `JobStore` | `MemoryJobStore` in `Session` |
| `observer` | `Observer` | Audit observations dropped |
| `approvals` | `Approvals` | Interactive gates suspend |
| `transcript` | `Transcript` | `MemoryTranscript` in `Session` |
| `sessions` | `SessionAccess` | Peer-session tools absent |
| `mcp_servers` | `MCPServers` | Session starts servers declared by its explicit workspace lease |
| `file_leases` | `FileLeases` | No cross-session mutation coordination |
| `permissions` | `PermissionPolicy` | Built-in evaluator |
| `prompt_composer` | `PromptComposer` | Catalogue `system_prompt` template |
| `tools` | `BaseTool` or `ToolGrant` sequence | No supplied tools |
| `toolset` | Complete `BaseTool` sequence | Built-in registry filtered by the agent || `hooks` | Any combination of the three hook protocols | None |
| `middleware` | `ToolMiddleware` sequence | None |
| `compaction` | `Compaction` | Token-bounded recent working set |
| `compaction_preparation` | `CompactionPreparation` | Observational-memory preparation in `Session` and the daemon; direct compaction in bare `AgentRuntime` |
| `continuations` | `ContinuationPolicy` | Active tuning allowances |
| `synchronize_resources` | Async callable | No synchronization |
| `related_turns` | Async turn reader | `read_turn` unavailable |
| `goal_listener` | Goal callback | No callback |
| `goal_review_journal` | `GoalReviewJournal` | Review events stream without product transcript persistence |

`SessionComponents` additionally owns `checkpoints`, `attachments`, `credentials`, `workspace`, and `tracer_provider`.

The `tools` field accepts bare tools or `ToolGrant` values. A `Session` built with `tools=[...]` merges them into the components. A tool granted later, through `Session.grant_tool`, is described to the model by an appended conversation message rather than a schema change. See [Granting a tool to a session](customization.md#granting-a-tool-to-a-session).

## Cache stability

Components are fixed for a runtime because the model-visible tool schemas and static instructions form the provider-cache prefix. Runtime controls such as steering, permission-mode changes, locations, and goal state are append-only or applied at execution boundaries; none rewrites an earlier model message. A custom prompt composer should produce the same output until the application explicitly calls `Session.refresh_prompt()`.
