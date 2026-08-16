Why this call is happening, in the words the person watching reads. Required on every call — without it the call appears as an icon and nothing else.

- It is a label the user sees, not private metadata: write the **why**, not the what — the arguments already show the what.
- Use a few words, one flat clause of intent, no final punctuation: "Fixing the token regression in auth", never "Auth: fix the token regression". A colon is fine (`file_path:line` or a ratio); inline Markdown renders, so put identifiers in backticks where that sharpens the why.
- Prefer "Verifying the auth fix didn't regress the session tests" to "Running the test suite", and "Finding every caller of `connect()` before changing its signature" to "Searching for Foo".
