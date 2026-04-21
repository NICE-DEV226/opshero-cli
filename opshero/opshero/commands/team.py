"""
Team command — manage team context and projects from the CLI.

Usage:
  opshero team                        # show team overview + active project
  opshero team projects               # list all team projects
  opshero team switch <name_or_id>    # set active project (persists in config)
  opshero team analyses               # show team-wide analyses
  opshero team project [id]           # show project details + recent analyses
  opshero team clear                  # clear active project context

Workflow example (DevOps/DevSecOps team):
  opshero team                        # see team: "Acme Corp" | project: "api-gateway"
  opshero team projects               # list: api-gateway, frontend, infra-k8s
  opshero team switch infra-k8s       # switch to infra project
  opshero analyze                     # analysis auto-linked to infra-k8s project
  opshero team analyses               # see all team failures across projects
"""

import asyncio
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opshero.api import APIError, AuthError, OpsHeroClient
from opshero.config import Config

console = Console()
err_console = Console(stderr=True)

_C = "#00d4ff"   # cyan
_A = "#00ff87"   # acid
_B = "#ffb020"   # amber
_D = "#ff4444"   # danger


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 60:    return f"{diff}s ago"
        if diff < 3600:  return f"{diff // 60}m ago"
        if diff < 86400: return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return iso[:10] if iso else "—"


def _confidence_color(c: float) -> str:
    if c >= 0.80: return _A
    if c >= 0.55: return _B
    return _D


def _conf_bar(c: float, width: int = 8) -> str:
    filled = round(c * width)
    bar = "█" * filled + "░" * (width - filled)
    col = _confidence_color(c)
    return f"[{col}]{bar} {c:.0%}[/{col}]"


def _role_badge(role: str) -> str:
    return {
        "owner": f"[bold {_B}]owner[/bold {_B}]",
        "admin": f"[{_C}]admin[/{_C}]",
        "member": "[dim]member[/dim]",
    }.get(role, f"[dim]{role}[/dim]")


# ── Group ──────────────────────────────────────────────────────────────────────

@click.group("team", invoke_without_command=True)
@click.pass_context
def team_group(ctx: click.Context):
    """
    Manage your team context and projects.

    \b
    Examples:
      opshero team                   # team overview + active project
      opshero team projects          # list all projects
      opshero team switch api-gw     # set active project
      opshero team analyses          # team-wide analysis feed
      opshero team project <id>      # project details
    """
    if ctx.invoked_subcommand is None:
        asyncio.run(_team_overview())


# ── opshero team (overview) ────────────────────────────────────────────────────

async def _team_overview() -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    if cfg.user_tier not in ("team", "enterprise"):
        console.print(
            f"\n  [{_B}]Team features require a Team plan.[/{_B}]\n"
            f"  Upgrade at [{_C}]https://opshero.me/dashboard/upgrade[/{_C}]\n"
        )
        raise SystemExit(0)

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Loading team…[/{_C}]"):
            try:
                team_data = await client.get_my_team()
            except AuthError as e:
                err_console.print(f"[{_D}]Auth error:[/{_D}] {e}")
                raise SystemExit(1)
            except APIError as e:
                err_console.print(f"[{_D}]API error:[/{_D}] {e}")
                raise SystemExit(1)

        team = team_data.get("team")
        if not team:
            console.print(
                f"\n  [dim]You are not part of a team yet.[/dim]\n"
                f"  Create one at [{_C}]https://opshero.me/dashboard/team[/{_C}]\n"
            )
            raise SystemExit(0)

        # Cache team info
        cfg.team_id = team.get("id")
        cfg.team_name = team.get("name")
        cfg.save()

        # Fetch projects
        try:
            projects_data = await client.get_team_projects()
            projects = projects_data.get("projects", [])
        except APIError:
            projects = []

    # ── Team header ────────────────────────────────────────────────────────────
    members = team.get("members", [])
    invitations = team.get("invitations", [])
    member_count = len(members)

    active_proj_line = ""
    if cfg.active_project_id:
        active_proj_line = (
            f"\n  [dim]Active project[/dim]  "
            f"[bold {_A}]{escape(cfg.active_project_name or cfg.active_project_id)}[/bold {_A}]"
            f"  [dim](use [bold]opshero team switch <name>[/bold] to change)[/dim]"
        )
    else:
        active_proj_line = (
            f"\n  [dim]No active project[/dim]  "
            f"[dim](use [bold]opshero team switch <name>[/bold] to set one)[/dim]"
        )

    console.print()
    console.print(Panel(
        f"\n  [bold white]{escape(team['name'])}[/bold white]"
        f"  [dim]·[/dim]  [{_C}]{member_count} member{'s' if member_count != 1 else ''}[/{_C}]"
        f"  [dim]·[/dim]  [{_B}]{len(projects)} project{'s' if len(projects) != 1 else ''}[/{_B}]"
        + (f"  [dim]·[/dim]  [{_B}]{len(invitations)} pending invite{'s' if len(invitations) != 1 else ''}[/{_B}]" if invitations else "")
        + active_proj_line
        + "\n",
        title=f"[bold {_C}]Team[/bold {_C}]",
        border_style=f"{_C} dim",
        padding=(0, 2),
    ))

    # ── Members table ──────────────────────────────────────────────────────────
    if members:
        console.print()
        tbl = Table(
            show_header=True,
            header_style=f"bold {_C}",
            border_style="dim",
            expand=False,
        )
        tbl.add_column("Member",  min_width=18)
        tbl.add_column("Role",    width=10)
        tbl.add_column("Joined",  width=12, justify="right")

        for m in members:
            login = m.get("github_login", "?")
            role  = m.get("role", "member")
            joined = _fmt_date(m.get("joined_at"))
            tbl.add_row(
                f"[bold]@{escape(login)}[/bold]",
                _role_badge(role),
                f"[dim]{joined}[/dim]",
            )
        console.print(tbl)

    # ── Projects summary ───────────────────────────────────────────────────────
    if projects:
        console.print()
        console.print(f"  [bold {_B}]Projects[/bold {_B}]")
        tbl2 = Table(show_header=True, header_style=f"bold {_B}", border_style="dim", expand=False)
        tbl2.add_column("Project",   min_width=20)
        tbl2.add_column("Repo",      min_width=22)
        tbl2.add_column("Members",   width=9,  justify="right")
        tbl2.add_column("Analyses",  width=10, justify="right")
        tbl2.add_column("This week", width=10, justify="right")

        for p in projects:
            is_active = p.get("id") == cfg.active_project_id
            name_str = (
                f"[bold {_A}]▶ {escape(p['name'])}[/bold {_A}]"
                if is_active
                else escape(p["name"])
            )
            tbl2.add_row(
                name_str,
                f"[dim]{escape(p.get('github_repo') or '—')}[/dim]",
                str(p.get("member_count", 0)),
                f"[{_A}]{p.get('total_analyses', 0)}[/{_A}]",
                f"[{_C}]{p.get('analyses_this_week', 0)}[/{_C}]",
            )
        console.print(tbl2)

    console.print(
        f"\n  [dim]Commands: [bold]opshero team projects[/bold]  ·  "
        f"[bold]opshero team switch <name>[/bold]  ·  "
        f"[bold]opshero team analyses[/bold][/dim]\n"
    )


# ── opshero team projects ──────────────────────────────────────────────────────

@team_group.command("projects")
def team_projects_cmd():
    """List all team projects with stats."""
    asyncio.run(_list_projects())


async def _list_projects() -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(f"[{_D}]Not authenticated.[/{_D}] Run [bold {_C}]opshero login[/bold {_C}]")
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Fetching projects…[/{_C}]"):
            try:
                data = await client.get_team_projects()
            except APIError as e:
                err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

    projects = data.get("projects", [])

    if not projects:
        console.print(
            f"\n  [dim]No projects yet.[/dim]\n"
            f"  Create one at [{_C}]https://opshero.me/dashboard/team[/{_C}]\n"
        )
        return

    console.print()
    tbl = Table(
        show_header=True,
        header_style=f"bold {_C}",
        border_style="dim",
        expand=True,
    )
    tbl.add_column("#",          width=3,  justify="right", style="dim")
    tbl.add_column("Project",    min_width=20)
    tbl.add_column("Repository", min_width=24)
    tbl.add_column("Members",    width=9,  justify="right")
    tbl.add_column("Analyses",   width=10, justify="right")
    tbl.add_column("This week",  width=10, justify="right")
    tbl.add_column("Status",     width=10, justify="center")

    for i, p in enumerate(projects, 1):
        is_active = p.get("id") == cfg.active_project_id
        name_str = (
            f"[bold {_A}]▶ {escape(p['name'])}[/bold {_A}]"
            if is_active
            else f"[white]{escape(p['name'])}[/white]"
        )
        status_str = f"[{_A}]active[/{_A}]" if is_active else "[dim]—[/dim]"

        tbl.add_row(
            str(i),
            name_str,
            f"[dim]{escape(p.get('github_repo') or '—')}[/dim]",
            str(p.get("member_count", 0)),
            f"[{_A}]{p.get('total_analyses', 0)}[/{_A}]",
            f"[{_C}]{p.get('analyses_this_week', 0)}[/{_C}]",
            status_str,
        )

    console.print(tbl)
    console.print(
        f"\n  [dim]Switch project: [bold]opshero team switch <name>[/bold][/dim]\n"
    )


# ── opshero team switch ────────────────────────────────────────────────────────

@team_group.command("switch")
@click.argument("project", required=False)
def team_switch_cmd(project: Optional[str]):
    """
    Set the active project context.

    \b
    All subsequent analyses will be linked to this project.
    The context persists between sessions.

    \b
    Examples:
      opshero team switch api-gateway
      opshero team switch              # interactive picker
    """
    asyncio.run(_switch_project(project))


async def _switch_project(project_name: Optional[str]) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(f"[{_D}]Not authenticated.[/{_D}] Run [bold {_C}]opshero login[/bold {_C}]")
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Fetching projects…[/{_C}]"):
            try:
                data = await client.get_team_projects()
            except APIError as e:
                err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

    projects = data.get("projects", [])

    if not projects:
        console.print(f"\n  [dim]No projects available.[/dim]\n")
        raise SystemExit(0)

    # Find by name or ID (fuzzy)
    chosen = None
    if project_name:
        needle = project_name.lower()
        # Exact match first
        for p in projects:
            if p["name"].lower() == needle or p["id"] == project_name:
                chosen = p
                break
        # Partial match
        if not chosen:
            matches = [p for p in projects if needle in p["name"].lower()]
            if len(matches) == 1:
                chosen = matches[0]
            elif len(matches) > 1:
                err_console.print(
                    f"[{_B}]Ambiguous — {len(matches)} projects match '{project_name}':[/{_B}]"
                )
                for m in matches:
                    console.print(f"  [dim]{m['name']}[/dim]")
                raise SystemExit(1)
            else:
                err_console.print(f"[{_D}]Project not found:[/{_D}] {project_name}")
                console.print(f"  [dim]Available: {', '.join(p['name'] for p in projects)}[/dim]")
                raise SystemExit(1)

    # Interactive picker if no name given
    if not chosen:
        console.print()
        tbl = Table(show_header=True, header_style=f"bold {_C}", border_style="dim")
        tbl.add_column("#", width=3, justify="right")
        tbl.add_column("Project", min_width=20)
        tbl.add_column("Repo", min_width=22)
        tbl.add_column("Analyses", width=10, justify="right")

        for i, p in enumerate(projects, 1):
            is_active = p.get("id") == cfg.active_project_id
            name_str = f"[bold {_A}]▶ {escape(p['name'])}[/bold {_A}]" if is_active else escape(p["name"])
            tbl.add_row(str(i), name_str, f"[dim]{escape(p.get('github_repo') or '—')}[/dim]", str(p.get("total_analyses", 0)))
        console.print(tbl)

        while True:
            try:
                pick = console.input(
                    f"\n  [dim]Select project [[bold]1-{len(projects)}[/bold]] (or q to quit): [/dim]"
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [dim]Cancelled.[/dim]")
                raise SystemExit(0)
            if pick in ("q", "quit", ""):
                console.print("  [dim]Cancelled.[/dim]")
                raise SystemExit(0)
            try:
                idx = int(pick)
                if 1 <= idx <= len(projects):
                    chosen = projects[idx - 1]
                    break
                err_console.print(f"  [{_D}]Enter a number between 1 and {len(projects)}.[/{_D}]")
            except ValueError:
                err_console.print(f"  [{_D}]Enter a number.[/{_D}]")

    # Save to config
    cfg.active_project_id   = chosen["id"]
    cfg.active_project_name = chosen["name"]
    cfg.save()

    console.print(
        f"\n  [{_A}]✓[/{_A}] Active project set to  "
        f"[bold {_A}]{escape(chosen['name'])}[/bold {_A}]\n"
        f"  [dim]All analyses will now be linked to this project.[/dim]\n"
        f"  [dim]Repo: {escape(chosen.get('github_repo') or '—')}[/dim]\n"
    )


# ── opshero team clear ─────────────────────────────────────────────────────────

@team_group.command("clear")
def team_clear_cmd():
    """Clear the active project context."""
    cfg = Config.load()
    old = cfg.active_project_name or cfg.active_project_id
    cfg.active_project_id   = None
    cfg.active_project_name = None
    cfg.save()
    if old:
        console.print(f"\n  [{_A}]✓[/{_A}] Cleared active project  [dim](was: {escape(old)})[/dim]\n")
    else:
        console.print(f"\n  [dim]No active project was set.[/dim]\n")


# ── opshero team project <id> ──────────────────────────────────────────────────

@team_group.command("project")
@click.argument("project_id", required=False)
@click.option("--limit", "-n", default=10, show_default=True, help="Number of recent analyses to show")
def team_project_cmd(project_id: Optional[str], limit: int):
    """
    Show details and recent analyses for a project.

    \b
    If no project ID is given, uses the active project.
    """
    asyncio.run(_show_project(project_id, limit))


async def _show_project(project_id: Optional[str], limit: int) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(f"[{_D}]Not authenticated.[/{_D}] Run [bold {_C}]opshero login[/bold {_C}]")
        raise SystemExit(1)

    pid = project_id or cfg.active_project_id
    if not pid:
        err_console.print(
            f"[{_B}]No active project.[/{_B}] "
            f"Run [bold {_C}]opshero team switch[/bold {_C}] to set one, "
            f"or pass a project ID."
        )
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Loading project…[/{_C}]"):
            try:
                project = await client.get_project(pid)
                analyses_data = await client.get_project_analyses(pid, per_page=limit)
            except APIError as e:
                err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

    analyses = analyses_data.get("items", [])
    stats = project.get("stats", {})

    # ── Project header ─────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        f"\n  [bold white]{escape(project['name'])}[/bold white]\n"
        + (f"  [dim]{escape(project.get('description', ''))}[/dim]\n" if project.get("description") else "")
        + (f"  [dim]Repo:[/dim]  [{_C}]{escape(project.get('github_repo', '—'))}[/{_C}]\n" if project.get("github_repo") else "")
        + f"\n"
        + f"  [dim]Total analyses[/dim]  [{_A}]{stats.get('total_analyses', 0)}[/{_A}]"
        + f"  [dim]·[/dim]  [dim]This week[/dim]  [{_C}]{stats.get('analyses_this_week', 0)}[/{_C}]"
        + f"  [dim]·[/dim]  [dim]Avg confidence[/dim]  [{_B}]{stats.get('avg_confidence', 0):.0%}[/{_B}]"
        + "\n",
        title=f"[bold {_B}]Project[/bold {_B}]",
        border_style=f"{_B} dim",
        padding=(0, 2),
    ))

    # ── Top error categories ───────────────────────────────────────────────────
    top_cats = stats.get("top_error_categories", [])
    if top_cats:
        console.print(f"  [dim]Top error categories:[/dim]  " + "  ".join(
            f"[{_C}]{c['category']}[/{_C}] [dim]({c['count']})[/dim]"
            for c in top_cats[:5]
        ))
        console.print()

    # ── Recent analyses ────────────────────────────────────────────────────────
    if analyses:
        console.print(f"  [bold {_C}]Recent Analyses[/bold {_C}]  [dim](last {len(analyses)})[/dim]")
        tbl = Table(show_header=True, header_style=f"bold {_C}", border_style="dim", expand=True)
        tbl.add_column("ID",         width=10, style="dim")
        tbl.add_column("Pattern",    min_width=22)
        tbl.add_column("Category",   min_width=12)
        tbl.add_column("Confidence", min_width=14)
        tbl.add_column("By",         width=16)
        tbl.add_column("When",       width=12, justify="right")

        for a in analyses:
            conf = a.get("confidence", 0.0) or 0.0
            tbl.add_row(
                (a.get("id") or "")[:8],
                escape(a.get("pattern_id") or "[dim]no match[/dim]"),
                escape(a.get("detected_category") or "—"),
                _conf_bar(conf),
                f"[dim]@{escape(a.get('user_github_login', '?'))}[/dim]",
                f"[dim]{_fmt_date(a.get('created_at'))}[/dim]",
            )
        console.print(tbl)
    else:
        console.print(f"  [dim]No analyses yet for this project.[/dim]")

    console.print()


# ── opshero team analyses ──────────────────────────────────────────────────────

@team_group.command("analyses")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of analyses to show")
@click.option("--project", "-p", default=None, help="Filter by project name or ID")
def team_analyses_cmd(limit: int, project: Optional[str]):
    """
    Show team-wide analysis feed across all projects.

    \b
    Examples:
      opshero team analyses              # all team analyses
      opshero team analyses -n 50        # last 50
      opshero team analyses -p api-gw    # filter by project
    """
    asyncio.run(_team_analyses(limit, project))


async def _team_analyses(limit: int, project_filter: Optional[str]) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(f"[{_D}]Not authenticated.[/{_D}] Run [bold {_C}]opshero login[/bold {_C}]")
        raise SystemExit(1)

    # Resolve project filter
    project_id = None
    project_name = None
    if project_filter:
        async with OpsHeroClient(cfg) as client:
            try:
                data = await client.get_team_projects()
                projects = data.get("projects", [])
                needle = project_filter.lower()
                match = next(
                    (p for p in projects if p["name"].lower() == needle or p["id"] == project_filter
                     or needle in p["name"].lower()),
                    None
                )
                if match:
                    project_id = match["id"]
                    project_name = match["name"]
                else:
                    err_console.print(f"[{_D}]Project not found:[/{_D}] {project_filter}")
                    raise SystemExit(1)
            except APIError as e:
                err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Loading team analyses…[/{_C}]"):
            try:
                if project_id:
                    data = await client.get_project_analyses(project_id, per_page=limit)
                else:
                    data = await client.get_team_analyses(per_page=limit)
            except APIError as e:
                err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

    items = data.get("items", [])
    total = data.get("total", len(items))

    title_suffix = f" — {escape(project_name)}" if project_name else ""
    console.print()

    if not items:
        console.print(f"  [dim]No analyses found{title_suffix}.[/dim]\n")
        return

    tbl = Table(
        show_header=True,
        header_style=f"bold {_C}",
        border_style="dim",
        expand=True,
        row_styles=["", "dim"],
    )
    tbl.add_column("ID",         width=10, style="dim")
    tbl.add_column("Pattern",    min_width=22)
    tbl.add_column("Category",   min_width=12)
    tbl.add_column("Confidence", min_width=14)
    tbl.add_column("By",         width=16)
    tbl.add_column("When",       width=12, justify="right")

    for a in items:
        conf = a.get("confidence", 0.0) or 0.0
        tbl.add_row(
            (a.get("id") or "")[:8],
            escape(a.get("pattern_id") or "[dim]no match[/dim]"),
            escape(a.get("detected_category") or "—"),
            _conf_bar(conf),
            f"[dim]@{escape(a.get('user_github_login', '?'))}[/dim]",
            f"[dim]{_fmt_date(a.get('created_at'))}[/dim]",
        )

    console.print(tbl)
    console.print(
        f"\n  [dim]Showing {len(items)} of {total} total"
        + (f" in {escape(project_name)}" if project_name else " across team")
        + f"  ·  [bold]opshero history show <id>[/bold] for full detail[/dim]\n"
    )
