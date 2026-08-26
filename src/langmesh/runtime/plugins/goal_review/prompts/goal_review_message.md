The exact message shown in the chat with the goal-review label and used to open the working session's next turn. Required when the standing is `unmet`. When standing is not `unmet`, omit this field (`null`); the standing code is what says it does not apply.

Write directly to the working session in the person's language and second person. This field is relayed verbatim: no template adds context later, so include which minimum condition is unproven, what is already established, the concrete next action, the result that would prove it, and any constraint at risk of being lost. Be specific enough that the message could not describe a different goal.

When `goal_contract` is `needs_revision`, make replacing the goal through `update_goal` the first instruction. Preserve its purpose and every existing minimum condition, then state each additional checkable condition that the replacement must add before explaining the work to continue afterward.

Do not repeat a route that already failed twice, shrink the intended outcome, loosen a constraint, or invent a file, command, flag, or fact. Name a genuinely different route grounded in what you inspected.
