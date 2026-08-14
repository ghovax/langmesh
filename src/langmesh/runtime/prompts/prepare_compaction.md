# Preserve durable workspace knowledge before folding

Pause the current work and load the `observational-memory` skill. Use local foreground Bash as described there to review and, when necessary, update the active observational memory before this conversation is folded.

## Begin with what already exists

- Read and validate the current observations first unless you already inspected their current revision and entries during this handoff.
- Compare them with the durable knowledge in the conversation so you understand what should remain, change, replace, or disappear.
- If the observations are absent or invalid, inspect what can be recovered safely before rebuilding them.

## Preserve the signal

- Keep knowledge when it is likely to change how a future agent understands the continuing goal, chooses an approach, avoids a known failure, evaluates progress, or honors a still-binding directive.
- Omit information whose value ends with the action that produced it, can be recovered cheaply from the current workspace, merely narrates execution, duplicates a stronger entry, or no longer changes a future decision.
- Describe the durable principle, state, or consequence at the level that will remain useful after the present task wording is forgotten. The subject does not affect eligibility.
- Treat observational memory as current state rather than an activity log.

## Complete the handoff

- Follow the skill's safe update and validation protocol.
- Make at least one successful update or explicit no-change acknowledgement before ending this handoff; use as many Bash calls as inspection, correction, and validation require.
- After the observations are safely acknowledged, conclude normally and briefly. Do not resume the paused work yet.
