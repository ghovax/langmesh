Read and drive the live screen with one short Python script — a single program that both finds elements and acts on them.

- The script runs against one `target` (a window or browser tab) and goes through `screen`, an object bound to that place. Only the primitives that target supports exist; reaching for one it lacks raises `NameError` on that line. Read the exact primitive signatures from `primitives` in your context each turn, not from memory.
- First line is `from langmesh.screen import screen`; no bare-named primitives. It is a real program (loops, conditionals, comprehensions, functions). Use `screen.wait_for(query, seconds=...)` instead of a guessed pause.

Finding and acting:
- `find_many` returns ranked matches; `find_one` returns the single best match and raises when the top candidates cannot be told apart. Quote a visible label exactly, or describe the purpose in a full sentence.
- `near=` names a unique neighbour to separate identical controls: `find_one("the toggle", near="the label shown in that row")`.
- Act on the element a find returned, not on its id: `result = screen.find_one("the Save button"); screen.click(result)`.
- `type` returns `value`, the text read back from the field — check that; an empty `changed` is not a failed keystroke.
- `find_many` returning `[]` means nothing rose above the noise — wait, check the target, or quote a visible label; it is not proof the thing is absent.
- `limit` defaults to 8; small is right, raise it only to harvest a set you will filter yourself.
- On a page, finds also search the page's network traffic, and `evaluate` runs a script in the page's own session.

Use the exposed find, query and workflow guidance; the available primitives and their signatures are authoritative.

Arguments:
- `script` — The Python to run.
- `target` — The window or tab to run it in, by the id from the target list. Required.
- `explanation` — Why the task needs this.
