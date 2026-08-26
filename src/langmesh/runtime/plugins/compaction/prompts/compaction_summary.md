You are performing a **context checkpoint compaction**. Create a handoff summary for
another language model that will resume this exact task. The next model sees the
summary, the live system prompt, and the most recent turns kept verbatim — anything you
omit is lost.

This instruction is the current request. Every earlier message is the material you are
summarizing — do not continue that conversation, reply to its last user message, or
answer it in prose. Go through the whole conversation once, in order, and carry every
fact, decision, and obligation that can still affect the work, tied to the task or goal
it belongs to.

## Carry everything that still matters

- **Tasks** — every user request, stated or implied, with its intent, acceptance
  criteria, and scope or constraints.
- **Decisions** — every decision made and why: the approach chosen, rejected
  alternatives, and accepted tradeoffs.
- **Preferences** — the user's style, format, language, tone, and review expectations.
  Interpret the intention behind their instructions, not their wording.
- **Constraints** — hard rules, boundaries, exclusions, and commitments: what must not
  be done, what was promised, and what is out of scope.
- **Progress** — what has been completed, in what order, and what verified it.
  Distinguish finished from claimed but unverified.
- **Findings** — what was discovered that changes future work: root causes, evidence,
  measurements, and negative or failed results.
- **Methods** — the workflows, scripts, commands, and procedures that produced results,
  so they can be repeated.
- **Artifacts** — files, code, commands, data, references, and examples the next model
  needs, with paths, names, hashes, and URLs.
- **State** — environment and system state: running jobs, branches, services, ports, and
  anything unfinished in the workspace. Never write secret values.
- **Open items** — everything remaining, in priority order, with the concrete next step
  and any dependency or blocker.
- **Open questions** — questions still awaiting the user, unanswered risks, and the
  decision each one needs.
- **Risks** — assumptions that could be wrong, warnings, partial results, and anything
  that must be rechecked.

## Keep it exact

Keep names, paths, identifiers, and values exact. Preserve causal links and conditions.
Include numbers, limits, and thresholds exactly as stated.

## Safety rules

- **Do not invent** — mark uncertain facts as uncertain and say what would confirm them.
- **Do not flatten nuance** — preserve conditions, exceptions, warnings, and unfinished
  threads.
- **Do not strip the negative** — failed attempts, rejected options, and closed doors
  are information.
- **Do not drop the user's voice** — their instructions, corrections, and preferences
  are the highest-priority content.
- **Never include secrets** — name a secret only by what it protects, never its value.
- **Do not pad** — every line must carry information another model could act on.
- **Erase what is dead** — once the summary exists, finished bookkeeping that cannot
  affect a future turn may be omitted.

## Submit

Answer only by calling `submit_compaction_summary` with the entire handoff summary in
its `summary` field. Do not call any other tool, do not write prose, and do not continue
the conversation.
