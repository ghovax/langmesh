---
name: observational-memory
title: Retrieve and maintain the workspace's observational memory
description: Retrieve or deliberately maintain the active workspace or location's current observational memory when it can materially affect the work. Use progressive disclosure with a fresh disposable Semble index for semantic retrieval and SQLite for exact lookup and mutation.
---

# Observational memory

Observational memory is current workspace/location knowledge in `.agents/observations.sqlite`. It is distinct from Memories, which are user-recorded passages comparable to reusable skills. The registry belongs to the place itself, can be tracked in Git, and contains only current state; Git is its history. Never turn it into an activity log or preserve obsolete rows in the database.

Consulting and maintaining this ledger is a core task, not an optional step. Before anything else in every turn, read the relevant slice of the ledger so it shapes what you inspect, how you interpret it, and what you decide; revisit it periodically as state, plans, or findings change, and before closing a turn. Keep it current as part of the work itself: whenever you establish or learn something likely to change a future agent's decisions, record or update it promptly rather than deferring until fold or the end.

## Retrieve

For an exact id, ledger, or category, query SQLite directly. For semantic discovery, create a temporary directory with `mktemp -d`, export current rows as minified JSONL, configure `.sembleignore` in that directory to include the JSONL file, set `SEMBLE_CACHE_LOCATION` to a cache path inside the same temporary directory, and use either `semble search "query" "$temporary_directory" --content all --top-k 10` or Python's `SembleIndex.from_path(..., content=ContentType.ALL).search(...)`. Remove the temporary directory when finished. Build a fresh index for every lookup and never save or commit an index. Semble is deliberately cheap enough to use repeatedly and is unusually effective at locating related concepts, structures, and mechanisms: make aggressive assumptions about joined searches, reformulate and repeat queries freely, and combine semantic results with exact searches appropriate to the workspace to narrow and verify. Retrieve only relevant rows and verify consequential claims against the workspace when practical.

Each exported line should be a minified object containing `ledger`, `id`, `updated_at`, and the entry's own columns. Example export logic is `SELECT entry_id, category, claim, detail, evidence, standing, files, updated_at FROM observations` and the same column list against `directives`; rows are plain named columns, so select them directly, omit `NULL` optionals, and serialize with `json.dumps(value, ensure_ascii=False, separators=(",", ":"))`.

## Schema

The registry has `registry_meta(id INTEGER NOT NULL PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=0))` with exactly one row `(1, 0)` before its first mutation, and two separate ledger tables, each a table of one row per entry with explicit columns and no JSON:

```
CREATE TABLE observations(
  entry_id TEXT NOT NULL PRIMARY KEY CHECK(length(trim(entry_id))>0),
  category TEXT NOT NULL CHECK(category IN ('fact','decision','constraint','failure','artifact','open')),
  claim TEXT NOT NULL CHECK(length(trim(claim))>0),
  detail TEXT NOT NULL CHECK(length(trim(detail))>0),
  evidence TEXT CHECK(evidence IS NULL OR length(trim(evidence))>0),
  standing TEXT NOT NULL CHECK(standing IN ('verified','reported','inferred')),
  files TEXT CHECK(files IS NULL OR length(trim(files))>0),
  updated_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE directives(
  entry_id TEXT NOT NULL PRIMARY KEY CHECK(length(trim(entry_id))>0),
  kind TEXT NOT NULL CHECK(kind IN ('requirement','preference')),
  summary TEXT NOT NULL CHECK(length(trim(summary))>0),
  detail TEXT CHECK(detail IS NULL OR length(trim(detail))>0),
  occasion TEXT CHECK(occasion IS NULL OR length(trim(occasion))>0),
  files TEXT CHECK(files IS NULL OR length(trim(files))>0),
  updated_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX idx_observations_updated_at ON observations(updated_at);
CREATE INDEX idx_directives_updated_at ON directives(updated_at);
```

Keep the ledgers in their own tables so the two kinds can never be confused. The columns themselves, not a payload, hold the entry: an observation row carries `category` (`fact`, `decision`, `constraint`, `failure`, `artifact`, or `open`), non-empty `claim`, non-empty `detail`, optional non-empty `evidence`, `standing` (`verified`, `reported`, or `inferred`). A directive row carries `kind` (`requirement` or `preference`), non-empty `summary`, optional non-empty `detail`, optional non-empty `occasion`. A `files` column, when present, is the entry's file paths or references joined by newlines, each line a non-empty path; keep file references there rather than folding them into prose fields. Every required column above must be written and non-empty; omitting one is a schema error, not a silent gap — that is the point of columnar rows. The row owns `entry_id` and `updated_at`; they are their own columns, never buried in a value. Create the parent directory and schema only when a write is needed. Use `PRAGMA journal_mode=DELETE` and `PRAGMA synchronous=FULL` so the tracked database is self-contained and durable. Every publication is copy-on-write: construct a complete database at a uniquely named temporary path inside `.agents`, validate it, close its SQLite connection, then publish it with `os.replace(temporary_path, registry_path)`. Never mutate or initialize the final path in place, because filesystem subscribers must see only a complete committed replacement. Every `entry_id` is a stable identifier, unique within its ledger, written in readable kebab-case like `observational-memory-schema`. Never suffix an id with a version or generation marker such as `-v2`, `-2`, or a date: a label like that signals that the entry supersedes another, which is exactly the kind of chain the registry forbids. When a concept evolves, replace the existing row in place under its original id; only delete a row when the concept or directive no longer applies, and reuse a freed id only for the same concept.

## Mutate

Use Python's standard `sqlite3` module from Bash for non-trivial changes. Read the current registry and remember its revision, create a unique sibling temporary path, copy a valid database into it with SQLite's backup API or construct a corrected database there from salvageable validated rows, and mutate only that temporary database. Start `BEGIN IMMEDIATE`, verify that the copied revision is still the one reviewed, perform parameterized upserts and deletes against the correct ledger table (`observations` or `directives`), and increment the revision exactly once in the same transaction. An upsert writes every required column by name — `INSERT INTO observations(entry_id, category, claim, detail, evidence, standing, files, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_id) DO UPDATE SET ...` with each value bound as a parameter: `entry_id` and `updated_at` as their own columns, `files` as the newline-joined string when the entry cites files, and `NULL` for an absent optional field such as `evidence`. Set `updated_at` to an ISO 8601 UTC timestamp with an offset for each upsert. Validate every write before committing: every required column non-empty, `category`/`standing`/`kind` within their allowed sets, `files` a non-empty newline-separated series when present. Commit, run integrity and schema validation, close every connection, confirm that the final path still has the reviewed revision, and publish with `os.replace`. If the final revision changed concurrently, discard the temporary file and reconsider instead of overwriting it. A no-change acknowledgement still follows this copy-on-write protocol and increments only the revision. Never use replacement chains, tombstones, supersession fields, or per-entry revision histories.

## Judgment

Use future decision value as the subject-agnostic test. Keep knowledge when it is likely to change how a future agent understands the continuing goal, selects an approach, avoids a known failure, evaluates what remains, locates a consequential source of truth, or honors a still-binding directive. Preserve enough rationale and evidence to make that knowledge usable and checkable without preserving the whole episode that produced it.

Omit information when its value ends with the action that produced it, the current workspace reveals it cheaply without interpretation, it merely narrates execution or verification, it duplicates a stronger current entry, or it no longer changes any plausible future decision. Prefer replacement over accumulation: when new knowledge supersedes or absorbs an existing entry, replace that entry in place and delete what it supersedes instead of adding a near-duplicate. Keep the registry to a sensible current set; if the ledger is crowding with redundant or stale rows, consolidate rather than accumulate, because an overgrown ledger buries the signal a future agent needs. Abstract away incidental task wording while retaining the durable state, principle, consequence, or unresolved question. A directive belongs only when the user intended it to continue beyond the immediate request. Apply these tests identically to every subject and medium. Refer to this material as observations, observational memory, concepts, data, or the ledger in ordinary conversation; do not volunteer its SQLite or JSONL representation unless the user is working on the mechanism itself.
