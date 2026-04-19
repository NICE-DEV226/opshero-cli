"""
OpsHero CLI — entry point.
"""

import sys
import os

# ── Windows: force UTF-8 output before Rich loads ──────────────────────────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # UTF-8 code page
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns

from opshero import __version__
from opshero.commands.analyze import analyze_cmd
from opshero.commands.apply import apply_cmd
from opshero.commands.auth import login_cmd, logout_cmd, whoami_cmd
from opshero.commands.history import history_group
from opshero.commands.patterns import patterns_group
from opshero.commands.rerun import rerun_cmd
from opshero.commands.sync import sync_group
from opshero.commands.contribute import contribute_group

console = Console(highlight=False)

# ── Brand palette ──────────────────────────────────────────────────────────────
_C = "#00d4ff"    # --cyan
_A = "#00ff87"    # --acid
_B = "#ffb020"    # --amber

# ── Logo: OPS in brand cyan · HERO in brand acid ────────────────────────────────
#
#   ╔═╗╔═╗╔═╗  ·  ╦ ╦╔═╗╦═╗╔═╗
#   ║ ║╠═╝╚═╗     ╠═╣║╣ ╠╦╝║ ║
#   ╚═╝╩  ╚═╝     ╩ ╩╚═╝╩╚═╚═╝
#

_OPS_1 = f"[bold {_C}]╔═╗╔═╗╔═╗[/bold {_C}]"
_OPS_2 = f"[bold {_C}]║ ║╠═╝╚═╗[/bold {_C}]"
_OPS_3 = f"[{_C}]╚═╝╩  ╚═╝[/{_C}]"

_HRO_1 = f"[bold {_A}]╦ ╦╔═╗╦═╗╔═╗[/bold {_A}]"
_HRO_2 = f"[bold {_A}]╠═╣║╣ ╠╦╝║ ║[/bold {_A}]"
_HRO_3 = f"[{_A}]╩ ╩╚═╝╩╚═╚═╝[/{_A}]"

LOGO_LINE1 = f"  {_OPS_1}  [dim]·[/dim]  {_HRO_1}"
LOGO_LINE2 = f"  {_OPS_2}     {_HRO_2}"
LOGO_LINE3 = f"  {_OPS_3}     {_HRO_3}"

# ── Command sections ───────────────────────────────────────────────────────────

SECTIONS = [
    (
        _C, "▸  ANALYZE",
        [
            "analyze",
            r"analyze [file|-]",
            r"analyze --repo owner/repo",
            r"analyze --repo owner/repo --run <id>",
            r"apply   [id] [--fix N]",
            r"rerun",
            r"rerun   --repo owner/repo",
            r"rerun   --repo owner/repo --run <id> --failed-only",
            r"history [-n N] [-c cat]",
            r"history show <id>",
        ],
        [
            "Auto-fetch latest failed GitHub Actions run",
            "Analyze a log file or pipe stdin",
            "Pick a failed run from a specific repo",
            "Analyze a specific GitHub Actions run",
            "Apply a suggested fix from an analysis",
            "Re-trigger latest failed run (auto-detect repo)",
            "Re-trigger a failed run from a specific repo",
            "Re-trigger only failed jobs (faster)",
            "List past analyses (--limit, --category)",
            "Full detail for one analysis by ID",
        ],
    ),
    (
        _A, "▸  AUTH",
        ["login", "logout", "whoami"],
        [
            "Connect your GitHub account (device flow)",
            "Revoke stored credentials",
            "Show current user and tier",
        ],
    ),
    (
        _B, "▸  PATTERNS & SYNC",
        [
            "patterns sync",
            "patterns list",
            "patterns show <id>",
            "sync push",
            "sync pull",
            "sync status",
        ],
        [
            "Pull latest patterns from cloud",
            "List all cached patterns",
            "Show a specific pattern in detail",
            "Push local analyses to cloud",
            "Pull team analyses from cloud",
            "Show last sync state",
        ],
    ),
    (
        "magenta", "▸  COMMUNITY",
        [
            "contribute submit",
            r"contribute submit --file pattern.json",
            "contribute list",
        ],
        [
            "Submit a new pattern (interactive form)",
            "Submit a JSON pattern file directly",
            "List your contributions and review status",
        ],
    ),
    (
        "dim", "▸  CONFIG",
        ["config show", "config set api-url"],
        [
            "Print current CLI settings",
            "Change the backend API URL",
        ],
    ),
]


def _print_welcome() -> None:
    """Styled welcome screen shown when opshero is run without arguments."""

    # ── Logo panel ─────────────────────────────────────────────────────────────
    sub = Text()
    sub.append("  Hybrid CI/CD log analysis  ·  ", style="dim")
    sub.append("regex", style=f"bold {_C}")
    sub.append(" + ", style="dim")
    sub.append("AI engine", style=f"bold {_A}")
    sub.append(f"  ·  v{__version__}", style="dim")

    logo_body = (
        f"\n"
        f"{LOGO_LINE1}\n"
        f"{LOGO_LINE2}\n"
        f"{LOGO_LINE3}\n\n"
        f"  {sub}\n"
    )

    console.print()
    console.print(Panel(
        logo_body,
        border_style=f"{_C} dim",
        padding=(0, 2),
        expand=False,
    ))
    console.print()

    # ── Command sections ────────────────────────────────────────────────────────
    for color, heading, cmds, descs in SECTIONS:
        console.print(f"  [bold {color}]{heading}[/bold {color}]")

        tbl = Table(
            show_header=False,
            box=None,
            padding=(0, 2, 0, 4),
            expand=False,
        )
        tbl.add_column("cmd",  style="bold white", no_wrap=True, min_width=36)
        tbl.add_column("desc", style="dim", no_wrap=False)

        for cmd, desc in zip(cmds, descs):
            tbl.add_row(f"opshero {cmd}", desc)

        console.print(tbl)
        console.print()

    # ── Footer ─────────────────────────────────────────────────────────────────
    console.print(
        f"  [dim]Tip: [/dim][bold {_C}]opshero COMMAND --help[/bold {_C}]"
        f"[dim] for detailed usage of any command.[/dim]\n"
    )


# ── Root group ─────────────────────────────────────────────────────────────────

@click.group(
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version", message=f"opshero v{__version__}")
@click.pass_context
def cli(ctx: click.Context):
    """
    \b
    OpsHero — Hybrid CI/CD log analysis.
    Fast regex engine with AI engine fallback.

    Run 'opshero COMMAND --help' for command-specific help.
    """
    if ctx.invoked_subcommand is None:
        _print_welcome()


# ── Auth ────────────────────────────────────────────────────────────────────────

cli.add_command(login_cmd,  name="login")
cli.add_command(logout_cmd, name="logout")
cli.add_command(whoami_cmd, name="whoami")

# ── Core workflow ───────────────────────────────────────────────────────────────

cli.add_command(analyze_cmd,   name="analyze")
cli.add_command(apply_cmd,     name="apply")
cli.add_command(rerun_cmd,     name="rerun")
cli.add_command(history_group, name="history")

# ── Sub-groups ──────────────────────────────────────────────────────────────────

cli.add_command(patterns_group)
cli.add_command(sync_group)
cli.add_command(contribute_group)


# ── Config ─────────────────────────────────────────────────────────────────────

@cli.group("config")
def config_group():
    """View and modify CLI configuration."""


@config_group.command("show")
def config_show():
    """Show current CLI configuration."""
    from opshero.config import Config, CONFIG_FILE, DATA_DIR
    cfg = Config.load()

    console.print()

    tbl = Table(show_header=False, box=None, padding=(0, 2, 0, 2))
    tbl.add_column("key", style="dim",   width=14)
    tbl.add_column("val", style="white")

    tbl.add_row("Config",   str(CONFIG_FILE))
    tbl.add_row("Data dir", str(DATA_DIR))
    tbl.add_row("API URL",  cfg.api_url)
    tbl.add_row(
        "Account",
        f"[bold]@{cfg.github_login}[/bold]" if cfg.github_login else "[dim]not logged in[/dim]",
    )
    tbl.add_row("Tier",      f"[{_B}]{cfg.user_tier}[/{_B}]")
    tbl.add_row("Client ID", f"[dim]{cfg.client_id[:8]}…[/dim]")
    tbl.add_row("Patterns",  f"[{_A}]{cfg.patterns_count}[/{_A}] cached")

    console.print(tbl)
    console.print()


@config_group.command("set")
@click.argument("key", type=click.Choice(["api-url"]))
@click.argument("value")
def config_set(key: str, value: str):
    """Set a configuration value (api-url)."""
    from opshero.config import Config
    cfg = Config.load()

    if key == "api-url":
        value = value.rstrip("/")
        cfg.api_url = value
        cfg.save()
        console.print(f"  [dim]api-url[/dim] [dim]→[/dim] [{_C}]{value}[/{_C}]")


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
