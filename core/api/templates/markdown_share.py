# v1.1.0 - 2026-04-24 - Optional "Edit on console" link for authenticated workspace shares
"""HTML template for rendered markdown share pages."""

import secrets
from html import escape as html_escape

import markdown_it

from core.api.config import settings

# Safe markdown renderer: html=False escapes raw HTML (XSS prevention layer 1)
_md = markdown_it.MarkdownIt("default", {"html": False})


MAX_MARKDOWN_RENDER_SIZE = 2 * 1024 * 1024  # 2 MB — larger files fall back to raw streaming


def _console_base_url() -> str:
    return settings.effective_console_base_url.rstrip("/")


def render_markdown_page(
    raw_md: str,
    filename: str,
    is_authenticated: bool,
    edit_token: str | None = None,
) -> tuple[str, str]:
    """Render markdown as an HTML page with copy protection.

    Args:
        raw_md: Raw markdown source.
        filename: Display filename (escaped before output).
        is_authenticated: Whether the request carries a valid session.
        edit_token: If provided AND is_authenticated, render an
            "Edit on console" link to /share/edit/<token>. Caller is
            responsible for passing token only for editable workspace shares.

    Returns (html_page, csp_header_value).
    """
    html_body = _md.render(raw_md)
    safe_name = html_escape(filename, quote=True)
    nonce = secrets.token_urlsafe(16)

    # Auth-dependent sections
    if is_authenticated:
        copy_css = ""
        copy_js = ""
        edit_link_html = ""
        if edit_token:
            safe_token = html_escape(edit_token, quote=True)
            edit_link_html = (
                f'<a href="{_console_base_url()}/share/edit/{safe_token}" target="_blank" '
                f'rel="noopener noreferrer" '
                f'style="padding:6px 12px;background:#1B1917;color:#fff;border-radius:4px;'
                f'text-decoration:none;font-size:13px">Edit on console</a>'
            )
        toolbar_html = f'''
        <div class="toolbar" style="position:fixed;top:0;right:0;padding:12px;display:flex;gap:8px;z-index:10">
            {edit_link_html}
            <a href="?raw=1" download="{safe_name}"
               style="padding:6px 12px;background:#2563eb;color:#fff;border-radius:4px;text-decoration:none;font-size:13px">
                Download .md
            </a>
            <button onclick="window.print()"
                    style="padding:6px 12px;background:#059669;color:#fff;border-radius:4px;border:none;cursor:pointer;font-size:13px">
                Print / PDF
            </button>
        </div>'''
    else:
        copy_css = "user-select: none; -webkit-user-select: none;"
        copy_js = f'''<script nonce="{nonce}">
document.addEventListener('contextmenu', e => e.preventDefault());
document.addEventListener('dragstart', e => e.preventDefault());
document.addEventListener('copy', e => e.preventDefault());
</script>'''
        toolbar_html = ""

    page = f'''<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
    max-width: 800px; margin: 0 auto; padding: 2.5rem 1.5rem;
    line-height: 1.7; color: #1a1a2e; background: #fafafa;
    font-size: 16px;
    {copy_css}
}}
h1 {{ font-size: 1.8rem; margin: 2rem 0 1rem; color: #111; line-height: 1.3; }}
h2 {{ font-size: 1.4rem; margin: 1.8rem 0 0.8rem; color: #222; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.3rem; line-height: 1.3; }}
h3 {{ font-size: 1.15rem; margin: 1.3rem 0 0.6rem; color: #333; }}
h4 {{ font-size: 1rem; margin: 1rem 0 0.5rem; color: #444; font-weight: 600; }}
p {{ margin: 0.7rem 0; }}
ul, ol {{ margin: 0.7rem 0; padding-left: 1.8rem; }}
li {{ margin: 0.3rem 0; }}
li > ul, li > ol {{ margin: 0.2rem 0; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
pre {{ background: #f0f0f5; padding: 1rem; overflow-x: auto; border-radius: 6px; margin: 1rem 0; font-size: 0.85rem; line-height: 1.5; }}
code {{ font-family: 'SF Mono', Consolas, 'Liberation Mono', monospace; font-size: 0.88em; background: #f0f0f5; padding: 2px 6px; border-radius: 3px; }}
pre code {{ background: none; padding: 0; font-size: 0.85rem; }}
blockquote {{ border-left: 3px solid #2563eb; padding: 0.6rem 1.2rem; margin: 1rem 0; color: #555; background: #f8f8ff; border-radius: 0 4px 4px 0; }}
table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; font-size: 0.9rem; }}
th, td {{ border: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #f5f5f5; font-weight: 600; }}
tr:nth-child(even) {{ background: #fafafa; }}
img {{ max-width: 100%; border-radius: 4px; margin: 0.5rem 0; }}
hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 2rem 0; }}
strong {{ color: #111; }}
/* Tablet */
@media (max-width: 1024px) {{
    body {{ max-width: 100%; padding: 2rem 1.2rem; }}
}}
/* Mobile */
@media (max-width: 600px) {{
    body {{ padding: 1rem 0.8rem; font-size: 15px; }}
    h1 {{ font-size: 1.5rem; }}
    h2 {{ font-size: 1.25rem; }}
    pre {{ padding: 0.7rem; font-size: 0.8rem; }}
    th, td {{ padding: 6px 8px; font-size: 0.85rem; }}
    table {{ display: block; overflow-x: auto; }}
}}
/* Print — A4 clean paging */
@media print {{
    body {{
        user-select: auto !important; -webkit-user-select: auto !important;
        max-width: 100%; padding: 0; margin: 0;
        font-size: 11pt; line-height: 1.5; color: #000; background: #fff;
    }}
    @page {{
        size: A4; margin: 20mm 18mm 25mm 18mm;
    }}
    .toolbar {{ display: none !important; }}
    h1 {{ font-size: 18pt; margin-top: 0; }}
    h2 {{ font-size: 14pt; page-break-after: avoid; }}
    h3 {{ font-size: 12pt; page-break-after: avoid; }}
    p, li, blockquote {{ orphans: 3; widows: 3; }}
    pre, blockquote, table, img {{ page-break-inside: avoid; }}
    pre {{ background: #f5f5f5; border: 1px solid #ddd; font-size: 9pt; }}
    a {{ color: #000; text-decoration: underline; }}
    a[href]::after {{ content: " (" attr(href) ")"; font-size: 8pt; color: #666; }}
    a[href^="#"]::after {{ content: ""; }}
}}
</style>
</head>
<body>
{toolbar_html}
<article>{html_body}</article>
{copy_js}
</body>
</html>'''

    csp = (
        f"default-src 'none'; "
        f"style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; "
        f"img-src https: data:; "
        f"frame-ancestors 'none'"
    )
    return page, csp
