What this call says about changing anything, and what it needs beyond the confinement
listed in your context.

- Always include it. `mutates: false` when the call only inspects; `mutates: true` when
  it changes anything.
- Add `writes` and `reads` only for paths outside the confinement, and `network` only
  where the confinement denies it. A declaration containing only `mutates` asks for no
  extra access.
- A granted path stays granted for the whole session: ask for the narrowest thing that
  does the work, and never use a path granted for one purpose to do something else.
- Refused paths are refused outright: no request opens one, and asking again in other
  words is not a different question.
