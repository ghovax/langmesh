Read and drive the live screen by composing a short Python script — one program that both finds elements and acts on them.

The script runs against one `target` — a window or browser tab you name — and everything goes through `screen`, an object already bound to that place. Only the primitives that target supports exist in the script, so reaching for one it does not have raises a `NameError` on that line. The exact primitive signatures arrive in your context each turn under `primitives`; read them from there, not from memory.

The script's first line is `from langmesh.screen import screen`; there are no bare-named primitives. It is a real program — loops, conditionals, comprehensions and functions all apply — and a whole task fits in one call. `screen.wait_for(query, seconds=...)` blocks until something matches, instead of a guessed pause.

Finding and acting:

- `find_many` returns ranked matches; `find_one` returns the single best match and raises where the top candidates cannot be told apart. Quote a visible label exactly as it appears; otherwise describe the purpose in a full sentence.
- `near=` names a unique neighbour to separate identical controls — `find_one("the toggle", near="the label shown in that row")`.
- Act on the element a find returned, not on its id: `result = screen.find_one("the Save button"); screen.click(result)`.
- `type` returns `value`, the text read back from the field — that is what you check. An empty `changed` is not a failed keystroke.
- A `find_many` that returns `[]` means nothing rose above the noise — wait, check the target, or quote a visible label; it is not proof the thing is absent.
- `limit` defaults to 8 and small is right; raise it only to harvest a set you will filter yourself.
- On a page, finds also search the page's network traffic, and `evaluate` runs a script in the page's own session.

Use the exposed find, query, and workflow guidance. The available primitives and their signatures are authoritative.

This call takes these arguments:

- `script` — The Python to run.
- `target` — The window or tab to run it in, by the id from the target list. Required.
- `explanation` — Why the task needs this.
