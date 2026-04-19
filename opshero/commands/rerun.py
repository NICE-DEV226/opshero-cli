"""
Rerun command — re-trigger a failed GitHub Actions workflow run.

Usage:
  opshero rerun                          # auto-detect repo, pick from failed runs
  opshero rerun --repo owner/repo        # pick from failed runs of a specific repo
  opshero rerun --repo owner/repo --run 12345   # re-run a specific run ID
  opshero rerun --failed-only            # re-run only the failed jobs (faster)
"""

import asyncio
import re
import subprocess
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from opshero.api import APIError, AuthError, NotFoundError, OpsHeroClient
from opshero.config import Config

console = Console()
err_console = Console(stderr=True)

_C = "#00d4ff"
_A = "#00ff87"
_B = "#ffb020"
_D = "#ff4444"


def _detect_git_repo() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _format_run_age(updated_at: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(updated_at.rstrip("Z")).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 60:    return f"{diff}s ago"
        if diff < 3600:  return f"{diff // 60}m ago"
        if diff < 86400: return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return updated_at[:10] if updated_at else "?"


@click.command("rerun")
@click.option("--repo", "-r", default=None, metavar="OWNER/REPO",
              help="GitHub repo (auto-detected from git remote if omitted)")
@click.option("--run", "run_id", type=int, default=None, metavar="RUN_ID",
              help="Specific run ID to re-trigger (skips interactive picker)")
@click.option("--failed-only", is_flag=True,
              help="Re-run only the failed jobs instead of the entire workflow (faster)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def rerun_cmd(
    repo: Optional[str],
    run_id: Optional[int],
    failed_only: bool,
    yes: bool,
):
    """
    Re-trigger a failed GitHub Actions workflow run.

    \b
    Examples:
      opshero rerun                          # auto-detect repo, pick run
      opshero rerun --repo myorg/myrepo      # pick from specific repo
      opshero rerun --repo myorg/myrepo --run 98765   # re-run directly
      opshero rerun --failed-only            # only re-run failed jobs
    """
    asyncio.run(_rerun(repo, run_id, failed_only, yes))


async def _rerun(
    repo: Optional[str],
    run_id: Optional[int],
    failed_only: bool,
    yes: bool,
) -> None:
    cfg = Config.load()

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    # ── Resolve repo ──────────────────────────────────────────────────────────
    detected_repo = repo
    if not detected_repo:
        detected_repo = _detect_git_repo()
        if not detected_repo:
            err_console.print(
                f"[{_D}]Could not auto-detect GitHub repo.[/{_D}]\n"
                f"[dim]Run inside a git repo with a GitHub remote, "
                f"or use [bold]--repo owner/repo[/bold].[/dim]"
            )
            raise SystemExit(1)
        console.print(f"  [dim]Detected repo[/dim]  [{_C}]{detected_repo}[/{_C}]")

    owner, _, repo_name = detected_repo.partition("/")
    if not repo_name:
        err_console.print(f"[{_D}]Invalid repo format:[/{_D}] {detected_repo}  (expected: owner/repo)")
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:

        # ── Resolve run ID ────────────────────────────────────────────────────
        chosen_run_id: int
        chosen_run_num: int | str

        if run_id is not None:
            chosen_run_id  = run_id
            chosen_run_num = run_id
        else:
            # Fetch recent failed runs and let user pick
            with console.status(f"[{_C}]Fetching failed runs for [bold]{detected_repo}[/bold]…[/{_C}]"):
                try:
                    runs = await client.github_failed_runs(owner, repo_name, per_page=10)
                except NotFoundError:
                    err_console.print(
                        f"[{_D}]Repo not found or access denied:[/{_D}] {detected_repo}\n"
                        f"  [dim]Re-authenticate: [bold {_C}]opshero login[/bold {_C}][/dim]"
                    )
                    raise SystemExit(1)
                except APIError as e:
                    err_console.print(f"[{_D}]Error fetching runs:[/{_D}] {e}")
                    raise SystemExit(1)

            if not runs:
                console.print(
                    f"  [{_A}]✓[/{_A}] No failed runs in [bold]{detected_repo}[/bold]. Nothing to re-run!"
                )
                raise SystemExit(0)

            # Show table
            console.print()
            table = Table(
                show_header=True,
                header_style=f"bold {_D}",
                border_style="dim",
                expand=True,
            )
            table.add_column("#",        width=3,  justify="right", style="dim")
            table.add_column("Run",      width=9,  justify="right")
            table.add_column("Workflow / Commit", min_width=28)
            table.add_column("Branch",   min_width=14)
            table.add_column("Age",      width=10, justify="right")

            for idx, run in enumerate(runs, 1):
                wf  = escape(run.get("workflow_name") or run.get("name") or "?")
                sha = run.get("head_sha", "")[:7]
                br  = escape(run.get("head_branch") or "?")
                age = _format_run_age(run.get("updated_at", ""))
                run_num = run.get("run_number", "?")
                table.add_row(
                    str(idx),
                    f"#{run_num}",
                    f"{wf} [dim]({sha})[/dim]",
                    f"[{_C}]{br}[/{_C}]",
                    f"[dim]{age}[/dim]",
                )
            console.print(table)

            if len(runs) == 1:
                chosen = runs[0]
                console.print(
                    f"  [dim]Auto-selecting the only failed run: "
                    f"#[bold]{chosen.get('run_number')}[/bold][/dim]"
                )
            else:
                indices = [str(i) for i in range(1, len(runs) + 1)]
                while True:
                    try:
                        pick = console.input(
                            f"\n  [dim]Select run to re-trigger "
                            f"[[bold]{', '.join(indices)}[/bold]] "
                            f"(or q to quit): [/dim]"
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        console.print("\n  [dim]Cancelled.[/dim]")
                        raise SystemExit(0)
                    if pick in ("q", "quit", ""):
                        console.print("  [dim]Cancelled.[/dim]")
                        raise SystemExit(0)
                    try:
                        idx = int(pick)
                        if 1 <= idx <= len(runs):
                            chosen = runs[idx - 1]
                            break
                        err_console.print(f"  [{_D}]Enter a number between 1 and {len(runs)}.[/{_D}]")
                    except ValueError:
                        err_console.print(f"  [{_D}]Enter a number.[/{_D}]")

            chosen_run_id  = chosen["id"]
            chosen_run_num = chosen.get("run_number", chosen_run_id)

        # ── Confirm ───────────────────────────────────────────────────────────
        mode_label = "[dim](failed jobs only)[/dim]" if failed_only else "[dim](all jobs)[/dim]"
        console.print()
        console.print(Panel(
            f"\n  [bold]Repo[/bold]   [{_C}]{detected_repo}[/{_C}]\n"
            f"  [bold]Run[/bold]    [white]#{chosen_run_num}[/white]  [dim](id: {chosen_run_id})[/dim]\n"
            f"  [bold]Mode[/bold]   {mode_label}\n",
            title=f"[bold {_C}]Re-trigger GitHub Actions Run[/bold {_C}]",
            border_style=f"{_C} dim",
            padding=(0, 2),
            expand=False,
        ))

        if not yes:
            try:
                confirm = console.input(
                    f"  [dim]Re-trigger this run? [[bold {_A}]y[/bold {_A}]/N]: [/dim]"
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [dim]Cancelled.[/dim]")
                raise SystemExit(0)
            if confirm not in ("y", "yes"):
                console.print("  [dim]Aborted.[/dim]")
                raise SystemExit(0)

        # ── Execute rerun ─────────────────────────────────────────────────────
        with console.status(f"[{_C}]Triggering re-run…[/{_C}]"):
            try:
                result = await client.github_rerun(
                    owner, repo_name, chosen_run_id, failed_only=failed_only
                )
            except APIError as e:
                if e.status_code == 403:
                    err_console.print(
                        f"[{_D}]Permission denied.[/{_D}] "
                        f"Your GitHub token needs [bold]repo[/bold] scope.\n"
                        f"  [dim]Re-authenticate: [bold {_C}]opshero login[/bold {_C}][/dim]"
                    )
                elif e.status_code == 409:
                    err_console.print(
                        f"[{_B}]Run #{chosen_run_num} is already in progress or cannot be re-run.[/{_B}]"
                    )
                else:
                    err_console.print(f"[{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

        console.print(
            f"\n  [{_A}]✓[/{_A}] Re-run triggered!  "
            f"[dim]Run #{chosen_run_num} is now queued.[/dim]\n"
            f"  [dim]Track on GitHub: "
            f"[{_C}]https://github.com/{detected_repo}/actions[/{_C}][/dim]\n"
        )
