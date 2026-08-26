Read files from the workspace (or other readable paths) into this turn as text and, when the model can see images, as image content.

- Pass every path you need in one call. Relative paths resolve against the workspace.
- Text is returned in the tool result. Images are attached as media on the following reminder so you can actually see them.
- Binary formats that are not images are reported as unread rather than dumped as bytes.
- Stay inside what this session may read. Ask for extra paths in `access_request` when you need them.

Arguments:
- `paths` — The files to ingest, as a list of paths.
