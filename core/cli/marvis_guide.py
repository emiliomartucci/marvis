# v1.0.0 - 2026-06-04 - C1: `marvis guide` — the single callable "Marvis way" guide.
"""``marvis guide`` — print the one reference for how Marvis works.

The guide is a packaged markdown data-file (``core/cli/guides/marvis-way.md``)
loaded at runtime via ``importlib.resources`` so it resolves identically from a
source checkout and from an installed wheel. It is the SAME content published on
the web (so an agent can read it before installing anything); this command is
the offline, already-installed surface.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_doctor`` / ``marvis_telemetry``.
"""
from __future__ import annotations

import sys
from typing import Any

import typer

_PANEL_GUIDE = "Guide"
_GUIDE_PACKAGE = "core.cli.guides"
_GUIDE_FILE = "marvis-way.md"


def _load_guide() -> str:
    """Read the packaged guide markdown (works from checkout and from wheel)."""
    import importlib.resources as ir

    text = ir.files(_GUIDE_PACKAGE).joinpath(_GUIDE_FILE).read_text(encoding="utf-8")

    from core.cli._onboarding import inject_guide_completion_markdown

    return inject_guide_completion_markdown(text)


def _split_sections(text: str) -> dict[str, str]:
    """Map each lowercased H2 title → its section markdown (title line included)."""
    sections: dict[str, str] = {}
    current_key: str | None = None
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "".join(current)
            current_key = line[3:].strip().lower()
            current = [line]
        elif current_key is not None:
            current.append(line)
    if current_key is not None:
        sections[current_key] = "".join(current)
    return sections


def guide_cmd(
    section: str | None = typer.Option(
        None,
        "--section",
        "-s",
        help="Print only one section (e.g. frontmatter, lifecycle, adopt).",
    ),
    list_sections: bool = typer.Option(
        False, "--list", help="List the section names and exit."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the guide as JSON to stdout."
    ),
    no_pager: bool = typer.Option(
        False, "--no-pager", help="Print straight to stdout, never page."
    ),
) -> None:
    """Print the Marvis way: how projects, tasks, the graph and the brain fit together."""
    import click

    text = _load_guide()
    sections = _split_sections(text)

    if list_sections:
        for name in sections:
            click.echo(name)
        return

    if section:
        key = section.strip().lower()
        if key not in sections:
            from core.cli._runtime_ctx import err_console

            err_console.print(
                f"[red]No section '{section}'. Available: {', '.join(sections)}[/red]"
            )
            raise typer.Exit(2)
        text = sections[key]

    if json_out:
        import json

        if section:
            payload: dict[str, Any] = {
                "section": section.strip().lower(),
                "markdown": text,
            }
        else:
            payload = {"guide": text, "sections": list(sections)}
        click.echo(json.dumps(payload))
        return

    # Page for a human TTY; plain stdout when piped or asked. echo_via_pager
    # silently skips the pager when stdin isn't a TTY, so gate on stdout instead.
    if sys.stdout.isatty() and not no_pager:
        click.echo_via_pager(text)
    else:
        click.echo(text)


def register(app: typer.Typer) -> None:
    """Attach ``guide`` onto an existing Typer app."""
    app.command("guide", rich_help_panel=_PANEL_GUIDE)(guide_cmd)
