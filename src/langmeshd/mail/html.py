"""Render the agent's markdown as HTML email.

Inbound mail is HTML; outbound mail is HTML too. The markdown the agent writes is converted
here. `multipart/alternative` still carries the markdown as `text/plain` so a client that
cannot show HTML has something to read; HTML is the last part, which is the one clients prefer.
"""

from __future__ import annotations

import markdown

_DOCUMENT = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: system-ui, sans-serif; line-height: 1.45; color: #111; }
pre, code { font-family: ui-monospace, monospace; }
pre { overflow: auto; padding: 0.75em; background: #f4f4f5; }
a { color: #1565c0; }
</style>
</head>
<body>
<!--body-->
</body>
</html>
"""


def html_from_markdown(text: str) -> str:
    """GitHub-flavoured markdown as a small HTML document."""
    body = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html",
    )
    return _DOCUMENT.replace("<!--body-->", body, 1)
