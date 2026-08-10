# Newly available conversation memory

This memory finished recording after the conversation was already underway. Incorporate it from this point forward without restarting the turn, repeating completed work, or mentioning the update merely to acknowledge it.

The update carries the complete memory that has finished recording: every finding the work established and every instruction the person gave, replacing the information of any earlier memory update. An exchange still being recorded is withheld entirely rather than sent in part, and arrives in the first update after its fold completes. The records are append-only. An entry naming ids in `supersedes` replaces those earlier entries; a directive with `still_binding` set to false retires the instruction it supersedes.

## Instructions

```jsonl
{{ directives }}
```

## Findings

```jsonl
{{ observations }}
```
