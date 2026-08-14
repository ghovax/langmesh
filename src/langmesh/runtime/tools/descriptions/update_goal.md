Set the session's single goal: the outcome the work is for, and what would prove it reached.

A goal is not a task list. Set one where the user asks for a concrete end state that will take several calls, edits or checks to reach; skip it for a small one-shot answer. Set it early — at the point you understand what is being asked, not once you are nearly done.

You set the goal; you do not close it. Whether it is reached is read off the work by a separate review that judges it against what you wrote, and its verdict decides what happens next. Write the goal for that reader: a requirement it cannot check will never be marked met. Do not spend a turn declaring yourself finished — state what you did and what it showed, and let it be read. Replacing a goal is for a goal that changed, never for one that got hard.

The status you are shown (`active`, `satisfied`, `blocked`, `parked`, `cleared`) is read from that review, not set by you; a settled goal is not one to keep working at.

- `goal` — the end state, in one or two sentences, written so it is either true or not: "the importer handles paginated responses and the existing tests still pass", not "work on the importer".
- `purpose` — what that end state is for, in the user's terms; the reason a closed route can be told apart from a lost goal.
- `requirements` — the minimum conditions that must hold, each one something a reader could go and look at. Vague ones ("tests pass", "it works") make a goal that cannot be audited.

Write all three in the language the user is speaking.

This call takes these arguments:

- `goal` — The end state, written so it is either true or not.
- `purpose` — What that end state is for, in the user's terms.
- `requirements` — The minimum conditions already known, each one checkable.
- `explanation` — A short reason for setting it, in the words the user reads.