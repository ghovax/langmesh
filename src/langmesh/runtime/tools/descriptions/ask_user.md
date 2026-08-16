Ask the user one or more questions, and wait for the answers.

- Ask only where the answer changes the work. Where a safe default is clear, choose it, say what you chose, and continue.
- When one option is better, say so and put it first — the user should see your pick and why without reading every description.
- Custom answers are on by default: never add a redundant "Other" or catch-all option.
- An answer comes back as the selected label or typed text (a bare string); only a `multiple` question answers with an array.

Arguments:
- `questions` — list of question objects: "question" (full text), "header" (short label, ~30 chars), "options" (a list of {"label", "description"}), and two optional flags: "multiple" and "custom" (default true).
- `explanation` — short reason for asking, in the words the user reads.
