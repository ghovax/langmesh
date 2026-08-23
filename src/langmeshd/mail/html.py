"""Render the agent's markdown as HTML email.

Inbound mail is HTML; outbound mail is HTML too. The markdown the agent writes is converted
here. `multipart/alternative` still carries the markdown as `text/plain` so a client that
cannot show HTML has something to read; HTML is the last part, which is the one clients prefer.

Gmail strips `<style>` blocks, so colour and type live on the tags themselves.
"""

from __future__ import annotations

import markdown

_BODY_STYLE = "font-family:system-ui,sans-serif;line-height:1.45;color:#111;"
_PRE_STYLE = "overflow:auto;padding:0.75em;background:#f4f4f5;font-family:ui-monospace,monospace;"
_CODE_STYLE = "font-family:ui-monospace,monospace;"
_LINK_STYLE = "color:#1565c0;"


def html_from_markdown(text: str) -> str:
    """GitHub-flavoured markdown as a small HTML document."""
    body = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html",
    )
    body = body.replace("<pre>", f'<pre style="{_PRE_STYLE}">')
    body = body.replace("<code>", f'<code style="{_CODE_STYLE}">')
    body = body.replace("<a ", f'<a style="{_LINK_STYLE}" ')
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        f'<body style="{_BODY_STYLE}">{body}</body></html>'
    )
