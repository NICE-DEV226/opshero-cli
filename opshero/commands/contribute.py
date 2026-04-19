"""
opshero contribute — community pattern contribution commands.

  opshero contribute submit          Interactive form to submit a pattern suggestion
  opshero contribute submit --file   Submit a JSON pattern file directly
  opshero contribute list            List your past contributions and their status
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opshero.config import Config
from opshero.api import OpsHeroClient, AuthError, APIError

console = Console(highlight=False)

VALID_CATEGORIES = [
    "docker", "npm", "python", "github-actions", "gitlab-ci",
    "kubernetes", "terraform", "rust", "go", "java", "ci", "security", "other",
]

STATUS_STYLES = {
    "pending_review":  ("yellow", "⏳"),
    "approved":        ("green",  "✓"),
    "rejected":        ("red",    "✗"),
    "changes_requested": ("amber", "↩"),
    "promoted":        ("cyan",   "🚀"),
}


# ── Group ─────────────────────────────────────────────────────────────────────

@click.group("contribute")
def contribute_group():
    """Submit and track community pattern contributions."""


# ── submit ────────────────────────────────────────────────────────────────────

@contribute_group.command("submit")
@click.option(
    "--file", "-f", "json_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Submit a JSON pattern file instead of using the interactive form.",
)
def submit_cmd(json_file: Optional[Path]):
    """
    Submit a pattern suggestion for community review.

    \b
    Two modes:
      Interactive form  —  opshero contribute submit
      JSON file         —  opshero contribute submit --file my-pattern.json

    \b
    JSON file format:
      {
        "title": "npm ERESOLVE dependency conflict",
        "category": "npm",
        "description": "Detects npm install ERESOLVE errors ...",
        "example_log": "npm ERR! code ERESOLVE\\nnpm ERR! ...",
        "suggested_fix": "Run: npm install --legacy-peer-deps",
        "regex_hint": "ERESOLVE|could not resolve"
      }
    """
    asyncio.run(_submit(json_file))


async def _submit(json_file: Optional[Path]) -> None:
    cfg = Config.load()
    if not cfg.access_token:
        console.print("\n  [red]Not logged in.[/red] Run [bold]opshero login[/bold] first.\n")
        return

    if json_file:
        # ── File mode ─────────────────────────────────────────────────────
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"\n  [red]Invalid JSON file:[/red] {e}\n")
            return

        required = {"title", "category", "description", "example_log", "suggested_fix"}
        missing = required - set(data.keys())
        if missing:
            console.print(f"\n  [red]Missing required fields:[/red] {', '.join(sorted(missing))}\n")
            return

        payload = {
            "title": data["title"],
            "category": data["category"],
            "description": data["description"],
            "example_log": data["example_log"],
            "suggested_fix": data["suggested_fix"],
            "regex_hint": data.get("regex_hint"),
        }
        console.print(f"\n  Submitting pattern from [bold]{json_file.name}[/bold]…")

    else:
        # ── Interactive form ───────────────────────────────────────────────
        console.print()
        console.print(Panel(
            "  [bold cyan]Contribute a pattern to the OpsHero community library[/bold cyan]\n"
            "  [dim]Your submission will be reviewed by the OpsHero team.[/dim]",
            border_style="cyan dim",
            padding=(0, 1),
        ))
        console.print()

        title = click.prompt("  Pattern name (short, descriptive)", type=str).strip()
        if len(title) < 5:
            console.print("  [red]Title too short (min 5 chars)[/red]")
            return

        console.print(f"\n  Categories: {', '.join(VALID_CATEGORIES)}")
        category = click.prompt("  Category", type=str).strip().lower()
        if category not in VALID_CATEGORIES:
            console.print(f"  [yellow]Warning: '{category}' is not a standard category (continuing anyway)[/yellow]")

        console.print()
        description = click.prompt(
            "  Describe the error this pattern detects\n  (what causes it, how to identify it)",
            type=str,
        ).strip()
        if len(description) < 20:
            console.print("  [red]Description too short (min 20 chars)[/red]")
            return

        console.print()
        console.print("  [dim]Paste a real CI/CD log excerpt showing this error.[/dim]")
        console.print("  [dim]End input with a line containing only '---'[/dim]")
        console.print()
        log_lines: list[str] = []
        while True:
            line = input()
            if line == "---":
                break
            log_lines.append(line)
        example_log = "\n".join(log_lines).strip()
        if len(example_log) < 20:
            console.print("  [red]Example log too short (min 20 chars)[/red]")
            return

        console.print()
        suggested_fix = click.prompt(
            "  What command or change fixes this error?",
            type=str,
        ).strip()
        if len(suggested_fix) < 10:
            console.print("  [red]Fix description too short (min 10 chars)[/red]")
            return

        console.print()
        regex_hint = click.prompt(
            "  Regex pattern to detect this error [optional, press Enter to skip]",
            default="",
        ).strip() or None

        payload = {
            "title": title,
            "category": category,
            "description": description,
            "example_log": example_log,
            "suggested_fix": suggested_fix,
            "regex_hint": regex_hint,
        }

        console.print()
        console.print("  [bold]Summary[/bold]")
        tbl = Table(show_header=False, box=None, padding=(0, 2, 0, 4))
        tbl.add_column("k", style="dim", width=16)
        tbl.add_column("v", style="white")
        tbl.add_row("Title", payload["title"])
        tbl.add_row("Category", payload["category"])
        tbl.add_row("Log length", f"{len(payload['example_log'])} chars")
        tbl.add_row("Regex hint", payload["regex_hint"] or "[dim]none[/dim]")
        console.print(tbl)
        console.print()

        if not click.confirm("  Submit this contribution?", default=True):
            console.print("  [dim]Cancelled.[/dim]\n")
            return

    # ── API call ───────────────────────────────────────────────────────────
    try:
        async with OpsHeroClient(cfg) as client:
            result = await client.submit_contribution(payload)

        console.print()
        console.print(
            f"  [green]✓[/green] Contribution submitted! "
            f"ID: [bold cyan]{result['id']}[/bold cyan]"
        )
        console.print(
            "  [dim]Track status with:[/dim] [bold]opshero contribute list[/bold]\n"
        )

    except AuthError:
        console.print("\n  [red]Authentication failed.[/red] Run [bold]opshero login[/bold].\n")
    except APIError as e:
        console.print(f"\n  [red]Submission failed:[/red] {e}\n")


# ── list ──────────────────────────────────────────────────────────────────────

@contribute_group.command("list")
@click.option("--limit", "-n", default=20, show_default=True, help="Max contributions to show.")
def list_cmd(limit: int):
    """List your past pattern contributions and their review status."""
    asyncio.run(_list(limit))


async def _list(limit: int) -> None:
    cfg = Config.load()
    if not cfg.access_token:
        console.print("\n  [red]Not logged in.[/red] Run [bold]opshero login[/bold] first.\n")
        return

    try:
        async with OpsHeroClient(cfg) as client:
            data = await client.list_my_contributions(per_page=limit)
    except AuthError:
        console.print("\n  [red]Authentication failed.[/red] Run [bold]opshero login[/bold].\n")
        return
    except APIError as e:
        console.print(f"\n  [red]Error:[/red] {e}\n")
        return

    items = data.get("items", [])
    if not items:
        console.print(
            "\n  [dim]No contributions yet.[/dim] "
            "Run [bold]opshero contribute submit[/bold] to get started.\n"
        )
        return

    console.print()
    tbl = Table(
        show_header=True,
        header_style="bold dim",
        box=None,
        padding=(0, 2, 0, 2),
        expand=False,
    )
    tbl.add_column("Status",   style="bold", width=20)
    tbl.add_column("Category", style="dim",  width=14)
    tbl.add_column("Title",    style="white", no_wrap=False)
    tbl.add_column("Date",     style="dim",   width=12)

    for item in items:
        status_raw = item.get("status", "unknown")
        color, icon = STATUS_STYLES.get(status_raw, ("dim", "?"))
        status_label = Text(f"{icon} {status_raw.replace('_', ' ')}", style=color)

        date_raw = item.get("created_at", "")
        date = date_raw[:10] if date_raw else "—"

        tbl.add_row(
            status_label,
            item.get("category", "—"),
            item.get("title", "—"),
            date,
        )

    console.print(tbl)
    total = data.get("total", len(items))
    console.print(
        f"\n  [dim]{len(items)} of {total} contribution(s)[/dim]"
        + (" — run with [bold]-n N[/bold] for more" if total > len(items) else "")
        + "\n"
    )
