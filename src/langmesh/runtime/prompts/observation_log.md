# Conversation memory

This is the complete live memory at a context-fold boundary. It replaces earlier memory notices while the ledgers themselves remain append-only: entries corrected later stay stored but are omitted here in favor of their replacements.

**Build on these records. Do not build around them.**

- Work the record calls done is done, so do not do it again to check.
- An approach the record rules out stays ruled out, and the entry says how it failed.
- A path, a command, an identifier or a number here is the one to use, not a hint to go and find the real one.
- An entry says its own `standing` — `verified` was proven, `reported` was claimed by something, `inferred` was concluded — so never promote one to another.
- Where a `reported` or `inferred` entry matters to what you are about to do, check it.
- If something you need is genuinely absent, establish it and carry on, since absence from the record does not prove that nobody established it.

What you see is what nothing later replaced: an entry that was corrected is not shown, and the correction is.

The conversation messages kept before this record are the most recent ones that fit the space reserved for them, taken whole. Earlier turns are absent, and so is any turn too large to fit, so do not read the visible messages as the whole of what happened recently.

## What the person asked for

These are the instructions the person gave. A directive with `still_binding` set to false retires the instruction it supersedes; everything else governs the work and does not lapse merely because its original turn was folded away.

```jsonl
{{ directives }}
```

## What the work established

```jsonl
{{ observations }}
```
