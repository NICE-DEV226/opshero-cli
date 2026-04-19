"""
Patterns commands — sync and browse the local pattern cache.

opshero patterns sync   — download latest patterns from API
opshero patterns list   — list cached patterns
opshero patterns show <id>  — show a single pattern detail
"""

import asyncio
import json
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from opshero.api import APIError, OpsHeroClient
from opshero.config import Config, PATTERNS_CACHE_FILE

console = Console()


@click.group("patterns")
def patterns_group():
    """Manage and browse error patterns."""


# ── sync ───────────────────────────────────────────────────────────────────────

@patterns_group.command("sync")
@click.option("--force", is_flag=True, help="Force re-download even if up to date")
def sync_cmd(force: bool):
    """Download the latest patterns from OpsHero API."""
    cfg = Config.load()
    if not cfg.is_authenticated:
        console.print("[yellow]Not logged in — run [bold cyan]opshero login[/bold cyan] first.[/yellow]")
        raise SystemExit(1)
    asyncio.run(_sync(cfg, force))


async def _sync(cfg: Config, force: bool) -> None:
    async with OpsHeroClient(cfg) as client:
        # Check manifest first
        with console.status("[cyan]Checking pattern manifest…[/cyan]"):
            try:
                manifest = await client.get_sync_manifest()
            except APIError as e:
                console.print(f"[red]Error:[/red] {e}")
                raise SystemExit(1)

        server_count = manifest.get("count", 0)
        local_count = cfg.patterns_count

        if not force and local_count == server_count and cfg.has_patterns_cache:
            console.print(
                f"[green]Patterns up to date[/green] ({local_count} patterns). "
                "Use [bold]--force[/bold] to re-download."
            )
            return

        # Download all patterns (paginate if needed)
        all_patterns: list[dict] = []
        page = 1
        with console.status(f"[cyan]Downloading {server_count} patterns…[/cyan]"):
            while True:
                try:
                    data = await client.list_patterns(page=page, per_page=200)
                except APIError as e:
                    console.print(f"[red]Download error:[/red] {e}")
                    raise SystemExit(1)

                items = data.get("items", [])
                all_patterns.extend(items)

                if len(all_patterns) >= data.get("total", 0):
                    break
                page += 1

        cfg.save_patterns_cache(all_patterns)
        console.print(
            f"[green]Synced[/green] {len(all_patterns)} patterns → "
            f"[dim]{PATTERNS_CACHE_FILE}[/dim]"
        )


# ── list ───────────────────────────────────────────────────────────────────────

@patterns_group.command("list")
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option("--severity", "-s", default=None,
              type=click.Choice(["critical", "high", "medium", "low"]))
@click.option("--search", default=None, help="Search by name or ID")
def list_cmd(category: Optional[str], severity: Optional[str], search: Optional[str]):
    """List locally cached patterns."""
    cfg = Config.load()
    patterns = cfg.load_patterns_cache()

    if not patterns:
        console.print(
            "[yellow]No local cache.[/yellow] Run [bold cyan]opshero patterns sync[/bold cyan]"
        )
        return

    # Filter
    if category:
        patterns = [p for p in patterns if p.get("category") == category]
    if severity:
        patterns = [p for p in patterns if p.get("severity") == severity]
    if search:
        s = search.lower()
        patterns = [
            p for p in patterns
            if s in p.get("pattern_id", "").lower() or s in p.get("name", "").lower()
        ]

    if not patterns:
        console.print("[yellow]No patterns match your filters.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan", row_styles=["", "dim"])
    table.add_column("Pattern ID", style="cyan", no_wrap=True)
    table.add_column("Name", max_width=45)
    table.add_column("Category", style="blue")
    table.add_column("Severity", justify="center")
    table.add_column("Solutions", justify="right")

    _SEV_COLOR = {"critical": "red", "high": "yellow", "medium": "white", "low": "dim"}

    for p in patterns:
        sev = p.get("severity", "?")
        color = _SEV_COLOR.get(sev, "white")
        table.add_row(
            p.get("pattern_id", "?"),
            p.get("name", "?"),
            p.get("category", "?"),
            f"[{color}]{sev}[/{color}]",
            str(len(p.get("solutions", []))),
        )

    console.print(table)
    console.print(f"\n[dim]{len(patterns)} pattern(s) shown.[/dim]")


# ── show ───────────────────────────────────────────────────────────────────────

@patterns_group.command("show")
@click.argument("pattern_id")
def show_cmd(pattern_id: str):
    """Show details for a specific pattern."""
    cfg = Config.load()
    patterns = cfg.load_patterns_cache()

    match = next(
        (p for p in patterns if p.get("pattern_id") == pattern_id),
        None,
    )
    if not match:
        console.print(f"[red]Pattern not found:[/red] {pattern_id}")
        console.print("[dim]Run [bold]opshero patterns sync[/bold] to update your cache.[/dim]")
        raise SystemExit(1)

    console.print(f"\n[bold cyan]{match.get('pattern_id')}[/bold cyan]  v{match.get('version', '?')}")
    console.print(f"[bold]{match.get('name')}[/bold]")
    console.print(
        f"Category: [blue]{match.get('category')}[/blue]  "
        f"Severity: [yellow]{match.get('severity')}[/yellow]  "
        f"Subcategory: {match.get('subcategory', '—')}"
    )

    tags = match.get("tags") or []
    if tags:
        console.print("Tags: " + "  ".join(f"[dim]{t}[/dim]" for t in tags))

    solutions = match.get("solutions") or []
    if solutions:
        console.print()
        console.rule("[bold]Solutions[/bold]", style="dim")
        for s in solutions:
            console.print(
                f"  [bold]{s.get('rank')}. {s.get('title')}[/bold]  "
                f"[dim]risk={s.get('risk')}[/dim]"
            )
            console.print(f"     [dim]{s.get('explanation', '')}[/dim]")
            if s.get("command_template"):
                console.print(f"     [green]$ {s.get('command_template')}[/green]")
            console.print()

    meta = match.get("metadata") or {}
    stats = meta.get("stats") or {}
    if stats.get("matched_count", 0) > 0:
        console.print(
            f"[dim]Stats: matched={stats.get('matched_count', 0)}  "
            f"helpful={stats.get('helpful_count', 0)}  "
            f"success_rate={stats.get('success_rate') or '—'}[/dim]"
        )
