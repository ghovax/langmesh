---
name: consolidate-observations
description: Review and deliberately consolidate the workspace-owned observational memory when the user asks. Use only on explicit user invocation; never trigger it automatically or as background housekeeping.
---

# Consolidate observations

Tell the user you are beginning this explicit, user-controlled maintenance operation, then load the `observational-memory` skill completely and follow its retrieval, schema, and transaction protocol. Use Semble with a fresh disposable index to find semantically overlapping entries, and query SQLite directly to inspect the exact candidate rows.

Identify only material maintenance by future decision value. Merge duplication, replace entries made obsolete by stronger current knowledge, remove resolved questions whose answers no longer affect later work, reconcile contradictions, and delete directives the user replaced or lifted. Preserve an entry when it can still change how a future agent understands the continuing goal, chooses an approach, avoids a known failure, evaluates progress, locates a consequential source of truth, or honors a binding directive. Remove it when its value ended with the producing action, current state reveals it cheaply without interpretation, it merely narrates execution, or it no longer changes a plausible future decision. Apply this test identically to every subject and medium.

This registry is current state and Git supplies its history. Consolidate observations and directives independently by atomically updating the best stable row, merging duplicates into it, and deleting the redundant or obsolete rows. A directive remains only when it still expresses an ongoing user requirement or preference; do not reinterpret a task-specific request as a permanent preference.

Do not manufacture new facts from the registry. Keep standing explicit (`verified`, `reported`, or `inferred`) and retain evidence only when it gives a future agent an independently checkable proof route. If no material consolidation is justified, make no write and say so. Finish by reporting exactly what was merged, updated, deleted, and left unchanged.
