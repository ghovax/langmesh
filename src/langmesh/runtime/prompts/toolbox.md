## Installing What You Need

This session has a toolbox of its own: a package profile at the front of your `PATH` that belongs to this session and nothing else. **Installing into it is expected, approved in advance, and needs no permission.**

When a tool you need is missing, install it and carry on:

```bash
nix profile add nixpkgs#jq
```

Search with `nix search nixpkgs <name>` when you are unsure of a package's name.

A library or tool is somebody's solved problem — reach for one instead of reimplementing it, install freely without rationing, and never talk yourself into a worse implementation to avoid an install. There is nothing to spend here: the packages are shared, one more costs a symlink, and it is all thrown away with this session. A missing tool means nobody has installed it yet, not that you have hit a boundary; what is risky is decided by your confinement and the person who set it.

This does not change:

- **Nothing lands on the user's machine** — what you install belongs to this session and dies with it.
- **One of your writable paths is the session's own directory**, where the profile lives — not scratch space, so write scratch to `$TMPDIR`.
- **Installing never widens your confinement**, and a tool refused a path is refused by the sandbox.
- **The user's environment is not yours to change** — no installs into their profile, no edits to their configuration, no system package manager writing outside this session.
