Download raw bytes from a URL into the session's caller-owned artifact store.

- Use `fetch_url` to read a page as text; use this for a PDF, archive, dataset, or other raw payload.
- The result names an artifact identifier that the embedding can retrieve through its `Artifacts` port; the library never chooses a filesystem path.
- Waits up to `timeout` seconds inline; a transfer still running continues as a durable background job, while `hard_deadline` aborts the network request itself.

Arguments:

- `url` — A complete HTTP or HTTPS URL.
- `timeout` — How many seconds to wait inline before returning a background job identifier.
- `hard_deadline` — How many seconds before the transfer itself aborts.
- `background` — Return the background job identifier immediately.
