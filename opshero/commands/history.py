"""
History command — list and inspect past analyses from the cloud.

Usage:
  opshero history                   # recent 20 analyses
  opshero history --limit 50        # up to 50 entries
  opshero history --category deps   # filter by category
  opshero history --json            # machine-readable output
  opshero history show <id>         # full detail for one analysis
"""

import asyncio
import json
import sys
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from opshero.api import APIError, AuthError, OpsHeroClient
from opshero.config import Config

console = Console()
err_console = Console(stderr=True)

# ── Brand palette ──────────────────────────────────────────────────────────────
_C = "#00d4ff"    # --cyan
_A = "#00ff87"    # --acid
_B = "#ffb020"    # --amber
_D = "#ff4444"    # --danger


def _confidence_color(c: float) -> str:
    if c >= 0.80:
        return _A
    if c >= 0.55:
        return _B
    return _D


def _conf_bar(c: float, width: int = 8) -> str:
    filled = round(c * width)
    bar = "█" * filled + "░" * (width - filled)
    col = _confidence_color(c)
    return f"[{col}]{bar} {c:.0%}[/{col}]"


def _method_label(method: str) -> str:
    return {
        "regex":                f"[{_C}]Regex[/{_C}]",
        "groq_llm":             f"[{_A}]AI engine[/{_A}]",
        "regex_offline":        f"[{_C}]Regex[/{_C}]",
        "regex_low_confidence": f"[{_C}]Regex[/{_C}] [dim](low)[/dim]",
        "generic_fallback":     "[dim]Fallback[/dim]",
    }.get(method, f"[dim]{method or '—'}[/dim]")


def _short_id(id_str: str) -> str:
    return id_str[:8] if id_str else "?"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        days = seconds // 86400
        if days < 30:
            return f"{days}d ago"
        return dt.strftime("%b %d, %Y")
    except Exception:
        return iso[:16]


# ── history (list) ─────────────────────────────────────────────────────────────

@click.group("history", invoke_without_command=True)
@click.option("--limit", "-n", default=20, show_default=True, help="Max number of entries")
@click.option("--category", "-c", default=None, help="Filter by category (e.g. deps, network)")
@click.option("--method", "-m", default=None, help="Filter by engine: regex, groq_llm")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON array")
@click.pass_context
def history_group(
    ctx: click.Context,
    limit: int,
    category: Optional[str],
    method: Optional[str],
    as_json: bool,
):
    """
    List your recent cloud analysis history.

    \b
    Examples:
      opshero history              # last 20 analyses
      opshero history -n 50        # last 50
      opshero history -c deps      # filter by category
      opshero history show <id>    # full detail
    """
    if ctx.invoked_subcommand is None:
        asyncio.run(_list_history(limit, category, method, as_json))


async def _list_history(
    limit: int,
    category: Optional[str],
    method: Optional[str],
    as_json: bool,
) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    filters: dict = {}
    if category:
        filters["category"] = category
    if method:
        filters["match_method"] = method

    async with OpsHeroClient(cfg) as client:
        try:
            data = await client.list_analyses(per_page=min(limit, 100), **filters)
        except AuthError as e:
            err_console.print(f"[{_D}]Auth error:[/{_D}] {e}")
            raise SystemExit(1)
        except APIError as e:
            err_console.print(f"[{_D}]API error:[/{_D}] {e}")
            raise SystemExit(1)

    items = data.get("items") or data.get("analyses") or []
    total = data.get("total", len(items))

    if as_json:
        sys.stdout.write(json.dumps(items, indent=2, default=str) + "\n")
        return

    if not items:
        console.print(f"  [dim]No analyses found.[/dim]")
        return

    # ── Table ──────────────────────────────────────────────────────────────────
    table = Table(
        show_header=True,
        header_style=f"bold {_C}",
        border_style="dim",
        expand=True,
        row_styles=["", "dim"],   # alternating row brightness
    )
    table.add_column("ID",       style="dim",   width=10, no_wrap=True)
    table.add_column("Category", min_width=14)
    table.add_column("Pattern",  min_width=20)
    table.add_column("Confidence", min_width=14)
    table.add_column("Engine",   width=14)
    table.add_column("When",     width=12, justify="right")

    for item in items[:limit]:
        conf   = item.get("confidence", 0.0) or 0.0
        meth   = item.get("match_method", "")
        table.add_row(
            _short_id(item.get("id", "")),
            escape(item.get("detected_category") or "—"),
            escape(item.get("pattern_id") or "[dim]no match[/dim]"),
            _conf_bar(conf),
            _method_label(meth),
            _fmt_date(item.get("created_at")),
        )

    console.print()
    console.print(table)
    console.print(
        f"\n  [dim]Showing {min(limit, len(items))} of {total} total  ·  "
        f"[bold]opshero history show <id>[/bold] for full detail[/dim]"
    )


# ── history show <id> ──────────────────────────────────────────────────────────

@history_group.command("show")
@click.argument("analysis_id")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def history_show(analysis_id: str, as_json: bool):
    """Show full detail for a single analysis by ID (or ID prefix)."""
    asyncio.run(_show_analysis(analysis_id, as_json))


async def _show_analysis(analysis_id: str, as_json: bool) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        try:
            result = await client.get_analysis(analysis_id)
        except APIError as e:
            if e.status_code == 404 and len(analysis_id) < 32:
                try:
                    data = await client.list_analyses(per_page=100)
                    items = data.get("items") or data.get("analyses") or []
                    matches = [i for i in items if i.get("id", "").startswith(analysis_id)]
                    if not matches:
                        err_console.print(f"  [{_D}]Analysis not found:[/{_D}] {analysis_id}")
                        raise SystemExit(1)
                    if len(matches) > 1:
                        err_console.print(
                            f"  [{_B}]Ambiguous prefix — {len(matches)} matches:[/{_B}]"
                        )
                        for m in matches:
                            console.print(f"  [dim]{m['id']}[/dim]  {m.get('pattern_id', '?')}")
                        raise SystemExit(1)
                    result = await client.get_analysis(matches[0]["id"])
                except SystemExit:
                    raise
                except Exception as inner:
                    err_console.print(f"  [{_D}]Error:[/{_D}] {inner}")
                    raise SystemExit(1)
            else:
                err_console.print(f"  [{_D}]API error:[/{_D}] {e}")
                raise SystemExit(1)

    if as_json:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        return

    _render_detail(result)


def _render_detail(r: dict) -> None:
    pattern_id = r.get("pattern_id") or "no match"
    category   = r.get("detected_category") or "unknown"
    confidence = r.get("confidence", 0.0) or 0.0
    method     = r.get("match_method", "—")
    solutions  = r.get("solutions") or []
    causal     = r.get("causal_chain") or {}
    created_at = _fmt_date(r.get("created_at"))
    aid        = r.get("id", "")
    total_ms   = r.get("total_latency_ms", 0) or 0
    llm_model  = r.get("llm_model")
    llm_ms     = r.get("llm_latency_ms")

    border_col = _confidence_color(confidence)

    header = "\n".join([
        "",
        f"  [bold white]◉  {escape(pattern_id)}[/bold white]",
        f"     [dim]id[/dim]  [dim]{aid}[/dim]  [dim]·[/dim]  [dim]{created_at}[/dim]",
        f"     [dim]category[/dim]  [white]{escape(category)}[/white]"
        f"  [dim]·[/dim]  [dim]engine[/dim]  {_method_label(method)}",
        "",
        f"     [dim]confidence[/dim]  {_conf_bar(confidence, width=10)}",
        "",
    ])

    console.print()
    console.print(Panel(header, border_style=border_col, padding=(0, 1)))

    # Solutions
    if solutions:
        console.print()
        n = len(solutions)
        for i, sol in enumerate(solutions, 1):
            rank  = sol.get("rank", i)
            title = sol.get("title", "")
            expl  = sol.get("explanation", "")
            cmd   = sol.get("command") or sol.get("command_template", "")
            risk  = sol.get("risk", "low")
            conf  = sol.get("confidence", 0.0) or 0.0
            rev   = sol.get("reversible", True)

            sc       = _confidence_color(conf)
            risk_col = {
                "none": "dim", "low": _A, "medium": _B, "high": _D,
            }.get(risk, "dim")
            rev_text = f"[{_A}]reversible[/{_A}]" if rev else f"[{_B}]irreversible[/{_B}]"

            console.print(
                f"  [bold {_C}]▸ Fix {rank}/{n}[/bold {_C}]  [dim]—[/dim]  "
                f"[bold white]{escape(title)}[/bold white]  [{sc}]{conf:.0%}[/{sc}]"
            )
            console.rule(style="dim", characters="─")

            console.print(
                f"  [dim]risk[/dim] [{risk_col}]{risk}[/{risk_col}]"
                f"  [dim]·[/dim]  {rev_text}"
            )

            if expl:
                console.print(f"\n  [dim]{escape(expl)}[/dim]")
            if cmd:
                console.print()
                console.print(
                    Syntax(cmd, "bash", theme="monokai", background_color="default",
                           padding=(1, 4))
                )
            console.print()

    # Causal chain
    caused_by    = causal.get("caused_by") or []
    often_causes = causal.get("often_causes") or []
    if caused_by or often_causes:
        console.rule("[dim]causal chain[/dim]", style="dim", characters="─")
        if caused_by:
            parts = [f"[{_C}]{escape(p)}[/{_C}]" for p in caused_by]
            console.print("  [dim]caused by[/dim]   " + "  [dim]→[/dim]  ".join(parts))
        if often_causes:
            parts = [f"[{_B}]{escape(p)}[/{_B}]" for p in often_causes]
            console.print("  [dim]leads to[/dim]    " + "  [dim]→[/dim]  ".join(parts))
        console.print()

    # Footer
    footer = [f"[dim]⏱  {total_ms}ms[/dim]"]
    if llm_model:
        footer.append(f"[dim]◈  {escape(llm_model)}  {llm_ms}ms[/dim]")
    fb = r.get("user_feedback")
    if fb is not None:
        icon = f"[{_A}]✓ helpful[/{_A}]" if fb else f"[{_D}]✗ not helpful[/{_D}]"
        footer.append(f"[dim]feedback[/dim] {icon}")
    console.print("  " + "   ".join(footer))
    console.print()
