"""
Sync commands — push/pull analyses between local SQLite and backend.

opshero sync push   — upload offline analyses to backend (pro/team)
opshero sync pull   — download analyses from other machines (pro/team)
opshero sync status — show local vs remote count
"""

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import click
from rich.console import Console
from rich.table import Table

from opshero.api import APIError, OpsHeroClient
from opshero.config import ANALYSES_DB_FILE, Config

console = Console()


# ── Local SQLite store ─────────────────────────────────────────────────────────

def _ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id          TEXT PRIMARY KEY,
                log_hash    TEXT NOT NULL,
                log_size    INTEGER DEFAULT 0,
                client_id   TEXT NOT NULL,
                pattern_id  TEXT,
                confidence  REAL DEFAULT 0,
                match_method TEXT,
                category    TEXT,
                variables   TEXT DEFAULT '{}',
                solutions   TEXT DEFAULT '[]',
                latency_ms  INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL,
                synced      INTEGER DEFAULT 0
            )
        """)
        conn.commit()


@contextmanager
def _db(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    _ensure_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def save_offline_analysis(result: dict, cfg: Config) -> None:
    """Save an offline analysis result to local SQLite for later sync."""
    if not result.get("pattern_id") and not result.get("log_hash"):
        return
    import uuid
    row_id = str(uuid.uuid4())
    with _db(ANALYSES_DB_FILE) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO analyses
              (id, log_hash, log_size, client_id, pattern_id, confidence,
               match_method, category, variables, solutions, latency_ms, created_at, synced)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                row_id,
                result.get("log_hash", ""),
                result.get("log_size_chars", 0),
                cfg.client_id,
                result.get("pattern_id"),
                result.get("confidence", 0.0),
                result.get("match_method", "no_match"),
                result.get("detected_category"),
                json.dumps(result.get("extracted_vars", {})),
                json.dumps(result.get("solutions", [])),
                result.get("total_latency_ms", 0),
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        conn.commit()


# ── Commands ───────────────────────────────────────────────────────────────────

@click.group("sync")
def sync_group():
    """Sync analyses between local cache and backend (pro/team)."""


@sync_group.command("push")
@click.option("--dry-run", is_flag=True, help="Show what would be pushed without sending")
def push_cmd(dry_run: bool):
    """Upload offline analyses to the backend."""
    cfg = Config.load()
    if not cfg.is_authenticated:
        console.print("[yellow]Not logged in — run [bold cyan]opshero login[/bold cyan][/yellow]")
        raise SystemExit(1)
    from opshero.tier import require_feature
    require_feature(cfg.user_tier, "sync_enabled")
    asyncio.run(_push(cfg, dry_run))


async def _push(cfg: Config, dry_run: bool) -> None:
    # Collect unsynced rows
    with _db(ANALYSES_DB_FILE) as conn:
        rows = conn.execute(
            "SELECT * FROM analyses WHERE synced = 0 ORDER BY created_at"
        ).fetchall()

    if not rows:
        console.print("[dim]Nothing to push — no unsynced offline analyses.[/dim]")
        return

    console.print(f"Found [bold]{len(rows)}[/bold] unsynced analyses.")
    if dry_run:
        for r in rows:
            console.print(f"  [dim]{r['created_at']}[/dim]  {r['pattern_id'] or '—'}  {r['match_method']}")
        return

    # Build payload
    items = [
        {
            "log_hash": r["log_hash"],
            "log_size_chars": r["log_size"],
            "client_id": r["client_id"],
            "pattern_id": r["pattern_id"],
            "confidence": r["confidence"],
            "match_method": r["match_method"],
            "detected_category": r["category"],
            "extracted_vars": json.loads(r["variables"] or "{}"),
            "solutions": json.loads(r["solutions"] or "[]"),
            "total_latency_ms": r["latency_ms"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[cyan]Pushing {len(items)} analyses…[/cyan]"):
            try:
                result = await client.sync_push(items)
            except APIError as e:
                if e.status_code == 403:
                    console.print("[red]Sync push requires a Pro or Team plan.[/red]")
                else:
                    console.print(f"[red]Error:[/red] {e}")
                raise SystemExit(1)

    # Mark as synced
    row_ids = [r["id"] for r in rows]
    with _db(ANALYSES_DB_FILE) as conn:
        conn.executemany(
            "UPDATE analyses SET synced = 1 WHERE id = ?",
            [(rid,) for rid in row_ids],
        )
        conn.commit()

    console.print(
        f"[green]Pushed:[/green] {result.get('inserted', 0)} inserted, "
        f"{result.get('skipped', 0)} duplicates skipped."
    )


@sync_group.command("pull")
@click.option("--since", default=None, metavar="ISO_DATE",
              help="Pull analyses since this timestamp (default: last pull or 30 days ago)")
def pull_cmd(since: str | None):
    """Download analyses from other machines/clients."""
    cfg = Config.load()
    if not cfg.is_authenticated:
        console.print("[yellow]Not logged in — run [bold cyan]opshero login[/bold cyan][/yellow]")
        raise SystemExit(1)
    from opshero.tier import require_feature
    require_feature(cfg.user_tier, "sync_enabled")
    asyncio.run(_pull(cfg, since))


async def _pull(cfg: Config, since_str: str | None) -> None:
    from datetime import timedelta

    if since_str:
        since = since_str
    else:
        # Default: 30 days ago
        since = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()

    async with OpsHeroClient(cfg) as client:
        with console.status("[cyan]Fetching remote analyses…[/cyan]"):
            try:
                result = await client.sync_pull(since=since, client_id=cfg.client_id)
            except APIError as e:
                if e.status_code == 403:
                    console.print("[red]Sync pull requires a Pro or Team plan.[/red]")
                else:
                    console.print(f"[red]Error:[/red] {e}")
                raise SystemExit(1)

    items = result.get("items", [])
    if not items:
        console.print("[dim]No new analyses from other clients.[/dim]")
        return

    # Merge into local SQLite
    import uuid
    with _db(ANALYSES_DB_FILE) as conn:
        saved = 0
        for item in items:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO analyses
                      (id, log_hash, log_size, client_id, pattern_id, confidence,
                       match_method, category, variables, solutions, latency_ms,
                       created_at, synced)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        item.get("id") or str(uuid.uuid4()),
                        item.get("log_hash", ""),
                        item.get("log_size_chars", 0),
                        item.get("client_id", "remote"),
                        item.get("pattern_id"),
                        item.get("confidence", 0.0),
                        item.get("match_method"),
                        item.get("detected_category"),
                        json.dumps(item.get("extracted_vars", {})),
                        json.dumps(item.get("solutions", [])),
                        item.get("total_latency_ms", 0),
                        item.get("created_at", datetime.now(tz=timezone.utc).isoformat()),
                    ),
                )
                saved += 1
            except Exception:
                pass
        conn.commit()

    has_more = result.get("has_more", False)
    console.print(
        f"[green]Pulled:[/green] {saved} analyses saved locally."
        + (" [yellow](more available — run pull again)[/yellow]" if has_more else "")
    )


@sync_group.command("status")
def status_cmd():
    """Show sync status for this machine."""
    cfg = Config.load()

    # Local counts
    total_local = 0
    unsynced = 0
    if ANALYSES_DB_FILE.exists():
        with _db(ANALYSES_DB_FILE) as conn:
            total_local = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
            unsynced = conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE synced = 0"
            ).fetchone()[0]

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    table.add_row("Local analyses", str(total_local))
    table.add_row("Unsynced", f"[yellow]{unsynced}[/yellow]" if unsynced else "0")
    table.add_row("Client ID", cfg.client_id[:8] + "…")
    table.add_row("Patterns cached", str(cfg.patterns_count))

    if cfg.patterns_synced_at:
        table.add_row("Patterns synced", cfg.patterns_synced_at.strftime("%Y-%m-%d %H:%M"))

    console.print(table)

    if not cfg.is_authenticated:
        console.print("[dim]Not logged in — remote sync unavailable.[/dim]")
        return

    if cfg.user_tier == "free":
        console.print("[dim]Sync push/pull requires Pro or Team plan.[/dim]")
        return

    asyncio.run(_remote_status(cfg))


async def _remote_status(cfg: Config) -> None:
    async with OpsHeroClient(cfg) as client:
        try:
            s = await client.sync_status(cfg.client_id)
            console.print(f"Remote (this client): {s.get('local_count', 0)} analyses")
            console.print(f"Remote (other clients): {s.get('remote_count', 0)} analyses")
            if s.get("last_push_at"):
                console.print(f"Last push: {s['last_push_at'][:19]}")
        except APIError:
            console.print("[dim]Could not reach API for remote status.[/dim]")
