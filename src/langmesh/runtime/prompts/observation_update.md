# Newly available conversation memory

This memory finished recording after the conversation was already underway. Incorporate it from this point forward without restarting the turn, repeating completed work, or mentioning the update merely to acknowledge it.

The records are append-only. An entry naming ids in `supersedes` replaces those earlier entries; a directive with `still_binding` set to false retires the instruction it supersedes.

## Newly recorded instructions

```jsonl
{{ directives }}
```

## Newly recorded findings

```jsonl
{{ observations }}
```
