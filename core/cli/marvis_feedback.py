# v1.0.0 - 2026-05-28 - C-Support RI-6: `marvis feedback` — open an OSS GitHub issue
"""``marvis feedback`` — open a GitHub issue on the public MarvisX repo.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_hooks`` / ``marvis_mcp`` / ``marvis_doctor``.

Three-level fallback (RI-6), tried in order — there is NEVER a Personal Access
Token embedded in the binary:

1. **``gh`` CLI** (when ``gh`` is on PATH): runs ``gh issue create`` with the
   user's own auth. Zero token shipped, the issue is attributed to the user
   (the best product signal).
2. **Pre-filled issue URL** (when ``gh`` is absent): builds
   ``https://github.com/<owner>/<repo>/issues/new?title=&body=&labels=source:cli``
   (URL-encoded) and opens it in the browser. The user reviews and clicks
   Submit — that click IS the consent. Headless / no-browser falls back to
   printing the URL.
3. **GitHub App / server proxy with a token — DEFERRED.** Not implemented (a
   server token is a spam + secret-management liability).

Hard rules honored here:

- The full issue body — including the auto-attached ``marvis doctor`` output,
  CLI version and OS/arch — is shown to the user and confirmed BEFORE sending
  (unless ``--yes``). Never an auto-submit without a human in the loop.
- The diagnostic context is PII-scrubbed: the home directory collapses to
  ``~`` and the OS username is replaced with ``<user>`` before it leaves the
  machine.
- The 414 "URI too long" pitfall is handled: when the URL fallback is used and
  the encoded URL would exceed a conservative length budget, the body is
  truncated in the URL and the user is told to paste the rest (or, better, to
  install ``gh``).

No DB, no network from this module itself (``gh`` does its own auth/HTTP);
``marvis doctor`` is reused for the diagnostic context — its check logic is
NOT duplicated.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from pathlib import Path

import typer

from core.cli._runtime_ctx import console, err_console

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PANEL_FEEDBACK = "Support"

# Target OSS repo. Kept consistent with the project URLs in pyproject.toml
# ([project.urls] Source/Issues = https://github.com/marvisx/marvisx-oss).
MARVISX_OSS_REPO = "marvisx/marvisx-oss"

# The label that turns the issue stream into a product-signal feed.
FEEDBACK_LABEL = "source:cli"

# GitHub rejects very long request URIs (HTTP 414). GitHub's own documented
# practical ceiling for issue-template query params is ~8 KB; we stay well
# under it so the title + fixed query params always fit and only the body is
# trimmed when needed.
_MAX_URL_LEN = 6000


def register(app: typer.Typer) -> None:
    """Attach ``feedback`` onto an existing Typer app."""
    app.command("feedback", rich_help_panel=_PANEL_FEEDBACK)(feedback_cmd)


# ---------------------------------------------------------------------------
# Diagnostic context (reuse marvis doctor — never duplicate its logic)
# ---------------------------------------------------------------------------


def _scrub_pii(text: str) -> str:
    """Best-effort scrub of obvious PII from diagnostic text.

    Collapses the user's home directory to ``~`` and replaces the OS username
    wherever it appears (covers paths the home-dir rule misses, e.g. a username
    embedded in a node name). Conservative: we only touch values we are sure
    identify the machine's owner.
    """
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")

    username = ""
    try:
        username = os.getlogin()
    except OSError:
        username = ""
    if not username:
        username = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if username and len(username) >= 2:
        text = text.replace(username, "<user>")

    return text


def _collect_doctor_report() -> str:
    """Run the existing ``marvis doctor`` checks and render them as scrubbed text.

    Reuses ``core.cli.marvis_doctor`` check functions directly (no subprocess,
    no duplicated logic). Connectivity is skipped (offline) so this never hangs
    or makes a network call.
    """
    from core.cli import marvis_doctor as doctor

    checks: list[doctor.CheckResult] = []
    try:
        checks.append(doctor._check_os())
        checks.append(doctor._check_python())
        checks.append(doctor._check_install_manager())
        checks.append(doctor._check_cli_on_path())
        checks.append(doctor._check_config_dir())
        checks.append(doctor._check_config_parseable())
        checks.extend(doctor._check_data_files())
        checks.append(doctor._check_connectivity(offline=True))
        checks.extend(doctor._check_granite_model())
    except Exception as exc:  # noqa: BLE001 — diagnostics must never block feedback
        return f"(doctor diagnostics unavailable: {exc})"

    lines = [f"- {c.name}: {c.level.upper()} — {c.detail}" for c in checks]
    return _scrub_pii("\n".join(lines))


def _cli_version() -> str:
    """The CLI version string, sourced from the single definition in marvis_init."""
    from core.cli.marvis_init import CLI_VERSION

    return CLI_VERSION


def _os_arch() -> str:
    return f"{platform.system()} {platform.release()} / {platform.machine()}"


def _build_body(message: str, command: str | None) -> str:
    """Assemble the issue body: user message + auto-attached diagnostics."""
    doctor_report = _collect_doctor_report()
    parts = [
        "### What happened",
        "",
        message.strip() or "_(no description provided)_",
        "",
        "### Command that triggered it",
        "",
        f"`{command.strip()}`" if command and command.strip() else "_(not specified)_",
        "",
        "### Environment",
        "",
        f"- CLI version: `{_cli_version()}`",
        f"- OS / arch: `{_os_arch()}`",
        "",
        "### marvis doctor",
        "",
        "```",
        doctor_report,
        "```",
        "",
        "---",
        "_Filed via `marvis feedback`._",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Level 1 — gh CLI
# ---------------------------------------------------------------------------


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _build_gh_command(title: str, body_file: str) -> list[str]:
    """The exact ``gh issue create`` argv. Pure, so tests can assert it.

    No token here: ``gh`` uses the user's own stored auth.
    """
    return [
        "gh",
        "issue",
        "create",
        "--repo",
        MARVISX_OSS_REPO,
        "--title",
        title,
        "--body-file",
        body_file,
        "--label",
        FEEDBACK_LABEL,
    ]


def _submit_via_gh(title: str, body: str) -> bool:
    """Create the issue with ``gh``. Returns True on success.

    Writes the body to a temp file (``--body-file``) to avoid argv length
    limits and shell-quoting issues. Uses ``subprocess.run`` with an argv list
    (never ``shell=True``).
    """
    fd, body_path = tempfile.mkstemp(prefix="marvis-feedback-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        cmd = _build_gh_command(title, body_path)
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user-controlled binary
            cmd,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            url = proc.stdout.strip()
            console.print(f"[green]Issue created:[/green] {url}" if url else "[green]Issue created.[/green]")
            return True
        err_console.print(
            f"[yellow]`gh issue create` failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}[/yellow]"
        )
        return False
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Level 2 — pre-filled issue URL
# ---------------------------------------------------------------------------


def _build_issue_url(title: str, body: str) -> tuple[str, bool]:
    """Build the pre-filled ``/issues/new`` URL.

    Returns ``(url, truncated)``. If the fully-encoded URL would exceed
    ``_MAX_URL_LEN`` (the 414 pitfall), the body is truncated and a marker is
    appended; ``truncated`` is then True so the caller can tell the user to
    paste the rest.
    """
    base = f"https://github.com/{MARVISX_OSS_REPO}/issues/new"
    fixed = {"title": title, "labels": FEEDBACK_LABEL}

    def _compose(body_text: str) -> str:
        params = dict(fixed)
        params["body"] = body_text
        return f"{base}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"

    url = _compose(body)
    if len(url) <= _MAX_URL_LEN:
        return url, False

    # Binary-free, simple shrink: trim the body until the whole URL fits, then
    # append a truncation marker.
    marker = "\n\n_(truncated — paste the rest of your details here)_"
    # Reserve room for the marker once encoded.
    budget = len(body)
    while budget > 0:
        candidate = body[:budget] + marker
        url = _compose(candidate)
        if len(url) <= _MAX_URL_LEN:
            return url, True
        budget -= 256
    # Degenerate: even an empty body is too long (fixed params huge) — return
    # the bare URL with just the marker.
    return _compose(marker), True


def _open_or_print_url(url: str, truncated: bool) -> bool:
    """Open the URL in a browser, or print it when headless. Always 'succeeds'.

    The user reviews the pre-filled form and clicks Submit themselves — that is
    the consent step, so there is no auto-submit here.
    """
    if truncated:
        err_console.print(
            "[yellow]The diagnostic context was long, so the issue URL was "
            "truncated to avoid a 414 (URI too long) error. Paste the rest of "
            "your details into the form, or install `gh` for the full report.[/yellow]"
        )

    headless = not (sys.stdout.isatty() or os.environ.get("DISPLAY") or sys.platform == "darwin")
    opened = False
    if not headless:
        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001 — fall through to printing the URL
            opened = False

    if opened:
        console.print(
            "[green]Opened a pre-filled GitHub issue in your browser.[/green] "
            "Review it and click Submit to send."
        )
    else:
        console.print("Open this pre-filled issue, review it, and click Submit:")
        console.print(url)
    return True


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def feedback_cmd(
    message: str = typer.Option(
        "",
        "--message",
        "-m",
        help="What happened (the issue body). Prompted if omitted and interactive.",
    ),
    title: str = typer.Option(
        "",
        "--title",
        "-t",
        help="Issue title. Prompted if omitted and interactive.",
    ),
    command: str = typer.Option(
        "",
        "--command",
        "-c",
        help="The marvis command that triggered the problem (optional).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the body preview + confirmation prompt.",
    ),
) -> None:
    """Open a GitHub issue on the public MarvisX repo (gh → pre-filled URL).

    Auto-attaches a PII-scrubbed ``marvis doctor`` report, the CLI version and
    your OS/arch, shows you the full body, and asks before anything is sent.
    No tokens are ever embedded — ``gh`` uses your own auth, and the URL
    fallback lets you click Submit yourself.
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if not title:
        if interactive:
            title = typer.prompt("Titolo della issue").strip()
        if not title:
            err_console.print(
                "[red]A title is required. Pass --title or run interactively.[/red]"
            )
            raise typer.Exit(2)

    if not message and interactive:
        message = typer.prompt(
            "Cosa e' successo? (descrizione)", default=""
        ).strip()

    body = _build_body(message, command or None)

    # Show the FULL body before sending — always, regardless of channel.
    console.print()
    console.rule("[bold]Issue preview")
    console.print(f"[bold]Title:[/bold] {title}")
    console.print(f"[bold]Repo:[/bold]  {MARVISX_OSS_REPO}  (label: {FEEDBACK_LABEL})")
    console.print()
    console.print(body)
    console.rule()

    if not yes:
        if not interactive:
            err_console.print(
                "[red]Refusing to send without confirmation. Re-run with --yes "
                "(non-interactive) once you have reviewed the body above.[/red]"
            )
            raise typer.Exit(2)
        if not typer.confirm("Invio questo feedback?", default=True):
            console.print("[yellow]Annullato — niente inviato.[/yellow]")
            raise typer.Exit(0)

    # Level 1: gh CLI (preferred — user auth, attributed, no token).
    if _gh_available():
        if _submit_via_gh(title, body):
            return
        err_console.print("[yellow]Falling back to a pre-filled issue URL.[/yellow]")

    # Level 2: pre-filled URL (user clicks Submit = consent).
    url, truncated = _build_issue_url(title, body)
    _open_or_print_url(url, truncated)
    # Level 3 (server token / GitHub App) is intentionally NOT implemented.
