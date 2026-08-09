# Consolidate observational memory

The live observational memories below exceed their context budget. Make them smaller by merging related findings and replacing obsolete ones while preserving every fact future work may need.

**Answer by calling the `ObservationBatch` tool with only the replacement entries.** Each replacement must name every entry it replaces in `supersedes`; entries left unchanged must not be repeated.

Preserve exact paths, identifiers, commands, versions, measurements, failures, decisions and open work. Consolidation must reduce the record without generalizing away concrete information or changing a finding's standing. Keep the replacement fields informationally distinct: `claim` states the conclusion, `detail` adds mechanism, boundaries and consequences, and `evidence` adds an independently checkable route to proof; remove paraphrased overlap and use `null` when there is no distinct evidence.

```jsonl
{{ observations }}
```
