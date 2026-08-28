Fetch the content at a URL and convert it to the format you ask for.

- Use this for a URL you already know; use `search_web` to find one. Returns page text;
  handles JavaScript-rendered pages and common anti-bot walls through rendering
  fallbacks. A very large response is truncated inline and carries a `content_artifact`
  that the embedding can retrieve through its artifact store.
- Use `download` for a raw binary file. This tool only reads.
- Waits up to `timeout` seconds and returns content directly; a fetch still running
  moves to the background and its result reaches you when it lands, so a slow page never
  blocks your turn. `hard_deadline` is a separate network cutoff that aborts the request
  itself. `background=true` backgrounds it at once.

Arguments:

- `url` — A complete http or https URL. It is fetched exactly as you give it; nothing
  rewrites the scheme, so write https yourself where you mean https.
- `format` — "markdown" (the default), "text", or "html".
- `timeout` — How many seconds to wait inline before the fetch moves to the background.
  It does not abort the fetch.
- `hard_deadline` — How many seconds before the network request itself aborts.
- `background` — Skip the inline wait, and background the fetch at once.
