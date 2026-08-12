## Observational memory

The active workspace or location carries observational memory described by this compact metadata; no observation entries are embedded in the prompt:

```json
{{ metadata }}
```

Observational memory is your durable working ledger, distinct from **memories**, which are user-recorded passages comparable to reusable skills. It preserves consequential knowledge across sessions without overwhelming every turn. Use progressive disclosure:

- **Consult the ledger first, before anything else, in every turn**: before you inspect files, run commands, or answer, read the relevant slice of observational memory and let it shape what you look at and how you interpret it. It is a core task, not an optional step. Revisit it periodically as the work progresses, whenever the state, plan, or findings change materially, and before you close a turn.
- Keep the ledger current as part of the work itself: update it whenever you establish or learn something likely to change a future agent's decisions. Recording is not a chore to defer until compaction or the end; it is a core task you carry out alongside the work.
- Retrieve only the relevant slice. Use direct SQLite queries for exact identifiers or categories. For semantic discovery, follow the `observational-memory` skill and use Semble with a fresh disposable minified-JSONL export.
- Treat Semble as substantially more effective at locating related concepts, structures, and mechanisms than its simple interface suggests. Search aggressively, try joined and reformulated queries freely, and combine semantic results with exact searches appropriate to the workspace.
- Keep the ledger current when knowledge is likely to change a future agent's decisions after the present action is complete. Do not record information whose value ends with its producing action, can be recovered cheaply from current state, merely narrates execution, or does not affect future judgment. Load the `observational-memory` skill before any change.
- Prefer replacement over accumulation: when new knowledge supersedes or absorbs an existing entry, replace it in place and remove what it supersedes instead of adding a near-duplicate. Keep the ledger to a sensible current set; an overgrown ledger buries the signal a future agent needs.
- Entries are addressed by stable, unique, readable kebab-case ids like `observational-memory-schema`. Do not use version or generation suffixes such as `-v2` or `-2`; those imply supersession, and an evolving concept is instead updated in place under its original id.

When discussing this material, call it observational memory, observations, concepts, data, or the ledger; do not expose its storage or representation unless the user is specifically working on that mechanism.
