"""
Analyze command — the core command of the CLI.

Usage:
  opshero analyze                       # auto-detect git repo, fetch latest failed run
  opshero analyze <log_file>            # analyze a file
  cat build.log | opshero analyze -     # read from stdin
  opshero analyze --repo owner/repo     # pick from failed runs of a specific repo
  opshero analyze --repo owner/repo --run 12345  # analyze a specific run directly
  opshero analyze --repo owner/repo --branch main  # filter by branch

Options:
  --repo            GitHub repo in owner/repo format (skips git-remote auto-detect)
  --run             GitHub Actions run ID (skips run selector)
  --branch          Filter runs by branch name
  --offline         Use local pattern cache (no network)
  --output json     Machine-readable JSON output
  --feedback / --no-feedback   Prompt for thumbs up/down after showing result
  --ci              Alias for --no-feedback --output json (for CI scripts)
"""

import asyncio
import hashlib
import json
import re
import subprocess
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

from opshero.api import APIError, AuthError, NotFoundError, OpsHeroClient
from opshero.config import Config
from opshero.local_engine import analyze_offline

console = Console()
err_console = Console(stderr=True)

# ── Brand palette ──────────────────────────────────────────────────────────────
_C = "#00d4ff"    # --cyan
_A = "#00ff87"    # --acid
_B = "#ffb020"    # --amber
_D = "#ff4444"    # --danger


# ── Display helpers ────────────────────────────────────────────────────────────

def _confidence_color(c: float) -> str:
    if c >= 0.80:
        return _A
    if c >= 0.55:
        return _B
    return _D


def _conf_bar(c: float, width: int = 10) -> str:
    """Unicode block-fill confidence bar: ████████░░ 82%"""
    filled = round(c * width)
    bar = "█" * filled + "░" * (width - filled)
    col = _confidence_color(c)
    return f"[{col}]{bar}[/{col}] [{col}]{c:.0%}[/{col}]"


def _risk_badge(risk: str) -> str:
    return {
        "none":   "[dim]none[/dim]",
        "low":    f"[{_A}]low[/{_A}]",
        "medium": f"[{_B}]medium[/{_B}]",
        "high":   f"[bold {_D}]HIGH[/bold {_D}]",
    }.get(risk, risk)


def _method_badge(method: str) -> str:
    return {
        "regex":                f"[{_C}]Regex[/{_C}]",
        "groq_llm":             f"[{_A}]AI engine[/{_A}]",
        "regex_offline":        f"[{_C}]Regex[/{_C}] [dim](offline)[/dim]",
        "regex_low_confidence": f"[{_C}]Regex[/{_C}] [dim](low)[/dim]",
        "generic_fallback":     "[dim]Fallback[/dim]",
    }.get(method, f"[dim]{method}[/dim]")


def _detect_git_repo() -> Optional[str]:
    """
    Try to get the GitHub repo from the current directory's git remote.
    Returns 'owner/repo' or None.
    """
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


def _detect_current_branch() -> Optional[str]:
    """Return the current git branch name, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else None
    except Exception:
        pass
    return None


async def _detect_project_context(client) -> Optional[str]:
    """Detect project context for team users."""
    try:
        # Get team projects
        team_data = await client.get_team_projects()
        projects = team_data.get("projects", [])
        
        if not projects:
            return None
        
        # If only one project, use it automatically
        if len(projects) == 1:
            return projects[0]["id"]
        
        # Try to match by git repository
        current_repo = _detect_git_repo()
        if current_repo:
            for project in projects:
                if project.get("github_repo") == current_repo:
                    return project["id"]
        
        # If multiple projects and no automatic match, let user choose
        console.print("\n[bold]Multiple team projects found:[/bold]")
        for i, project in enumerate(projects, 1):
            repo_info = f" ({project['github_repo']})" if project.get('github_repo') else ""
            console.print(f"  {i}. {project['name']}{repo_info}")
        
        console.print("  0. No project (individual analysis)")
        
        while True:
            try:
                choice = input("\nSelect project (0-{}): ".format(len(projects)))
                choice_num = int(choice)
                if choice_num == 0:
                    return None
                elif 1 <= choice_num <= len(projects):
                    return projects[choice_num - 1]["id"]
                else:
                    console.print("[red]Invalid choice. Please try again.[/red]")
            except (ValueError, KeyboardInterrupt):
                return None
                
    except Exception:
        # If team project detection fails, continue without project context
        return None


def _format_run_age(updated_at: str) -> str:
    """Format a run timestamp as a relative age string."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(updated_at.rstrip("Z")).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 60:
            return f"{diff}s ago"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return updated_at[:10] if updated_at else "?"


# ── GitHub resolution ──────────────────────────────────────────────────────────

async def _resolve_github_log(
    client: "OpsHeroClient",
    repo: Optional[str],
    run_id: Optional[int],
    branch: Optional[str],
    output: str,
) -> tuple[str, dict]:
    """
    Resolve the log text and metadata from GitHub Actions.
    Returns (log_text, metadata_dict).
    """
    detected_repo = repo
    if not detected_repo:
        detected_repo = _detect_git_repo()
        if not detected_repo:
            err_console.print(
                f"[{_D}]Could not auto-detect GitHub repo.[/{_D}]\n"
                f"[dim]Run inside a git repo with a GitHub remote, or use [bold]--repo owner/repo[/bold].[/dim]"
            )
            raise SystemExit(1)
        if output == "rich":
            console.print(f"  [dim]Detected repo[/dim]  [{_C}]{detected_repo}[/{_C}]")

    if not branch:
        branch = _detect_current_branch()

    owner, _, repo_name = detected_repo.partition("/")
    if not repo_name:
        err_console.print(
            f"[{_D}]Invalid repo format:[/{_D}] [bold]{detected_repo}[/bold]  "
            "Expected: owner/repo"
        )
        raise SystemExit(1)

    if run_id is not None:
        if output == "rich":
            with console.status(f"[{_C}]Downloading logs for run #{run_id}…[/{_C}]"):
                try:
                    data = await client.github_run_logs(owner, repo_name, run_id)
                except NotFoundError:
                    err_console.print(f"[{_D}]Run #{run_id} not found in {detected_repo}.[/{_D}]")
                    raise SystemExit(1)
                except APIError as e:
                    err_console.print(f"[{_D}]Error fetching logs:[/{_D}] {e}")
                    raise SystemExit(1)
        else:
            data = await client.github_run_logs(owner, repo_name, run_id)

        if data.get("truncated") and output == "rich":
            console.print(f"  [{_B}]Note: logs were truncated (>500 KB).[/{_B}]")

        metadata = {
            "source": "github_actions",
            "repo": detected_repo,
            "run_id": run_id,
            "branch": branch or "",
        }
        return data["logs"], metadata

    if output == "rich":
        branch_hint = f" on [{_C}]{branch}[/{_C}]" if branch else ""
        with console.status(f"[{_C}]Fetching failed runs for [bold]{detected_repo}[/bold]{branch_hint}…[/{_C}]"):
            try:
                runs = await client.github_failed_runs(
                    owner, repo_name, branch=branch, per_page=10
                )
            except NotFoundError:
                err_console.print(
                    f"[{_D}]Repo not found or access denied:[/{_D}] {detected_repo}\n"
                    f"  [dim]Possible causes:[/dim]\n"
                    f"  [dim]1. Private repo — run [bold {_C}]opshero login[/bold {_C}] [dim]to refresh your token (needs 'repo' scope)[/dim]\n"
                    f"  [dim]2. Org repo — authorize OpsHero at:[/dim] "
                    f"[{_C}]github.com/settings/connections/applications[/{_C}]"
                )
                raise SystemExit(1)
            except APIError as e:
                if "401" in str(e) or "token" in str(e).lower():
                    err_console.print(
                        f"[{_D}]GitHub token expired or missing.[/{_D}] "
                        f"Re-authenticate: [bold {_C}]opshero login[/bold {_C}]"
                    )
                else:
                    err_console.print(f"[{_D}]Error fetching runs:[/{_D}] {e}")
                raise SystemExit(1)
    else:
        try:
            runs = await client.github_failed_runs(owner, repo_name, branch=branch, per_page=10)
        except APIError as e:
            sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
            raise SystemExit(1)

    if not runs:
        branch_msg = f" on branch '{branch}'" if branch else ""
        if output == "rich":
            console.print(
                f"  [{_A}]✓[/{_A}] No failed runs in [bold]{detected_repo}[/bold]{branch_msg}."
            )
        else:
            sys.stdout.write(json.dumps({"status": "no_failures", "repo": detected_repo}) + "\n")
        raise SystemExit(0)

    if output == "rich":
        console.print()
        table = Table(
            show_header=True,
            header_style=f"bold {_D}",
            border_style="dim",
            expand=True,
        )
        table.add_column("#",  width=3, justify="right", style="dim")
        table.add_column("Run", justify="right", width=9)
        table.add_column("Workflow / Commit", min_width=28)
        table.add_column("Branch", min_width=14)
        table.add_column("Age",  width=10, justify="right")

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
            chosen_run = runs[0]
            console.print(
                f"  [dim]Auto-selecting the only failed run: "
                f"#[bold]{chosen_run.get('run_number')}[/bold][/dim]"
            )
        else:
            indices = [str(i) for i in range(1, len(runs) + 1)]
            while True:
                try:
                    pick = console.input(
                        f"\n  [dim]Select run [[bold]{', '.join(indices)}[/bold]] "
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
                        chosen_run = runs[idx - 1]
                        break
                    err_console.print(f"  [{_D}]Enter a number between 1 and {len(runs)}.[/{_D}]")
                except ValueError:
                    err_console.print(f"  [{_D}]Enter a number.[/{_D}]")
    else:
        chosen_run = runs[0]

    chosen_id  = chosen_run["id"]
    chosen_num = chosen_run.get("run_number", chosen_id)

    if output == "rich":
        with console.status(f"[{_C}]Downloading logs for run #{chosen_num}…[/{_C}]"):
            try:
                data = await client.github_run_logs(owner, repo_name, chosen_id)
            except NotFoundError:
                err_console.print(
                    f"[{_D}]No logs available for run #{chosen_num}.[/{_D}]\n"
                    f"  [dim]This usually means the workflow failed before any steps ran\n"
                    f"  (e.g. YAML syntax error, missing secrets, or run was cancelled).\n"
                    f"  Check on GitHub: [{_C}]github.com/{detected_repo}/actions/runs/{chosen_id}[/{_C}][/dim]"
                )
                raise SystemExit(1)
            except APIError as e:
                err_console.print(f"[{_D}]Error fetching logs:[/{_D}] {e}")
                raise SystemExit(1)
    else:
        try:
            data = await client.github_run_logs(owner, repo_name, chosen_id)
        except APIError as e:
            sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
            raise SystemExit(1)

    if data.get("truncated") and output == "rich":
        console.print(f"  [{_B}]Note: logs were truncated (>500 KB).[/{_B}]")

    metadata = {
        "source": "github_actions",
        "repo": detected_repo,
        "run_id": chosen_id,
        "run_number": chosen_num,
        "branch": chosen_run.get("head_branch") or branch or "",
        "workflow": chosen_run.get("workflow_name") or chosen_run.get("name") or "",
    }
    return data["logs"], metadata


# ── Main command ───────────────────────────────────────────────────────────────

@click.command("analyze")
@click.argument("log_file", type=click.Path(exists=False, allow_dash=True), default=None, required=False)
@click.option("--repo", "-r", default=None, metavar="OWNER/REPO",
              help="GitHub repo to fetch logs from (auto-detected from git remote if omitted)")
@click.option("--run", "run_id", type=int, default=None, metavar="RUN_ID",
              help="GitHub Actions run ID (skips interactive run picker)")
@click.option("--branch", "-b", default=None, metavar="BRANCH",
              help="Filter GitHub runs by branch name")
@click.option("--offline", is_flag=True, help="Use local pattern cache, skip API")
@click.option(
    "--output", "-o",
    type=click.Choice(["rich", "json"]),
    default="rich",
    help="Output format",
)
@click.option(
    "--feedback/--no-feedback",
    default=True,
    help="Prompt for helpful/not-helpful after showing result",
)
@click.option("--ci", is_flag=True, help="CI mode: --output json --no-feedback")
@click.option("--context", "-c", multiple=True, metavar="KEY=VALUE",
              help="Extra context passed to the API (e.g. --context env=staging)")
def analyze_cmd(
    log_file: Optional[str],
    repo: Optional[str],
    run_id: Optional[int],
    branch: Optional[str],
    offline: bool,
    output: str,
    feedback: bool,
    ci: bool,
    context: tuple,
):
    """
    Analyze a CI/CD log for errors and get fix suggestions.

    \b
    With no arguments, auto-detects the current git repo and fetches
    the latest failed GitHub Actions run:
      opshero analyze

    \b
    Pipe a log file, or pass a path:
      cat build.log | opshero analyze -
      opshero analyze build.log

    \b
    Target a specific repo or run:
      opshero analyze --repo myorg/myrepo
      opshero analyze --repo myorg/myrepo --run 98765
      opshero analyze --repo myorg/myrepo --branch main
    """
    if ci:
        output = "json"
        feedback = False

    ctx_dict: dict = {}
    for item in context:
        if "=" in item:
            k, _, v = item.partition("=")
            ctx_dict[k.strip()] = v.strip()

    # ── Accept a full GitHub URL as the positional argument ────────────────────
    # e.g. opshero analyze https://github.com/owner/repo.git
    #      opshero analyze github.com/owner/repo
    if log_file and not repo:
        _gh_match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", log_file)
        if _gh_match:
            repo = _gh_match.group(1)
            log_file = None

    stdin_is_tty = sys.stdin.isatty()
    github_mode = (log_file is None and (repo is not None or run_id is not None or stdin_is_tty))

    if github_mode:
        asyncio.run(_analyze_github(repo, run_id, branch, offline, output, feedback, ctx_dict))
        return

    src = log_file if log_file else "-"
    try:
        if src == "-":
            raw_log = click.get_text_stream("stdin").read()
        else:
            with open(src, encoding="utf-8", errors="replace") as f:
                raw_log = f.read()
    except OSError as e:
        err_console.print(f"[{_D}]Cannot read file:[/{_D}] {e}")
        raise SystemExit(1)

    if not raw_log.strip():
        err_console.print(f"[{_B}]Warning:[/{_B}] Empty log input.")
        raise SystemExit(0)

    asyncio.run(_analyze(raw_log, offline, output, feedback, ctx_dict, metadata={}))


# ── GitHub mode entry point ────────────────────────────────────────────────────

async def _analyze_github(
    repo: Optional[str],
    run_id: Optional[int],
    branch: Optional[str],
    offline: bool,
    output: str,
    feedback: bool,
    context: dict,
) -> None:
    cfg = Config.load()

    if offline:
        err_console.print(
            f"[{_B}]GitHub mode is not compatible with --offline.[/{_B}]\n"
            "[dim]Supply a log file instead: [bold]opshero analyze <file>[/bold][/dim]"
        )
        raise SystemExit(1)

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        raw_log, metadata = await _resolve_github_log(
            client, repo, run_id, branch, output
        )

    if not raw_log.strip():
        err_console.print(f"[{_B}]Warning:[/{_B}] Fetched log is empty.")
        raise SystemExit(0)

    if output == "rich":
        console.print()

    await _analyze(raw_log, offline=False, output=output, show_feedback_prompt=feedback,
                   context=context, metadata=metadata)


# ── Core analysis logic ────────────────────────────────────────────────────────

async def _analyze(
    raw_log: str,
    offline: bool,
    output: str,
    show_feedback_prompt: bool,
    context: dict,
    metadata: dict,
) -> None:
    cfg = Config.load()
    result: dict

    if offline or not cfg.is_authenticated:
        if not offline and not cfg.is_authenticated:
            if output == "rich":
                console.print("[dim]Not logged in — running offline with local patterns.[/dim]")
        patterns = cfg.load_patterns_cache()
        if not patterns:
            err_console.print(
                f"[{_B}]No local pattern cache.[/{_B}] "
                f"Run [bold {_C}]opshero patterns sync[/bold {_C}] first."
            )
            raise SystemExit(1)
        result = analyze_offline(raw_log, patterns)

    else:
        log_client_id = hashlib.sha256(
            f"{cfg.client_id}:{raw_log}".encode()
        ).hexdigest()[:32]

        merged_context = {**context, **{f"gh_{k}": str(v) for k, v in metadata.items()}}

        async with OpsHeroClient(cfg) as client:
            # Detect project context for team users
            project_id = None
            try:
                project_id = await _detect_project_context(client)
            except Exception:
                pass  # Continue without project context if detection fails
            
            if output == "rich":
                source_label = ""
                if metadata.get("source") == "github_actions":
                    repo_str = metadata.get("repo", "")
                    run_num  = metadata.get("run_number") or metadata.get("run_id", "")
                    source_label = (
                        f" [dim]({repo_str} · run #{run_num})[/dim]"
                    )
                
                project_label = ""
                if project_id:
                    project_label = " [dim](team project)[/dim]"
                    
                with console.status(f"[{_C}]Analyzing log{source_label}{project_label}…[/{_C}]"):
                    try:
                        result = await client.analyze(
                            log=raw_log,
                            client_id=log_client_id,
                            context=merged_context,
                            project_id=project_id,
                        )
                    except AuthError as e:
                        err_console.print(f"[{_D}]Auth error:[/{_D}] {e}")
                        raise SystemExit(1)
                    except APIError as e:
                        err_console.print(f"[{_D}]API error:[/{_D}] {e}")
                        raise SystemExit(1)
            else:
                try:
                    result = await client.analyze(
                        log=raw_log,
                        client_id=log_client_id,
                        context=merged_context,
                        project_id=project_id,
                    )
                except Exception as e:
                    sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
                    raise SystemExit(1)

        if show_feedback_prompt and output == "rich" and result.get("id"):
            _render_result(result, output)
            await _prompt_feedback(cfg, result["id"])
            return

    _render_result(result, output)


# ── Rendering ──────────────────────────────────────────────────────────────────

def _render_result(result: dict, output: str) -> None:
    if output == "json":
        out = {k: v for k, v in result.items() if not k.startswith("_")}
        sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
        return

    pattern_id = result.get("pattern_id")
    confidence = result.get("confidence", 0.0) or 0.0
    method     = result.get("match_method", "no_match")
    category   = result.get("detected_category") or "unknown"
    solutions  = result.get("solutions") or []
    causal     = result.get("causal_chain")
    is_offline = result.get("_offline", False)
    llm_model  = result.get("llm_model")
    llm_ms     = result.get("llm_latency_ms")
    total_ms   = result.get("total_latency_ms", 0) or 0

    # ── No match ───────────────────────────────────────────────────────────────
    if not pattern_id:
        console.print()
        console.print(Panel(
            f"\n  [{_B}]◯  No matching pattern found[/{_B}]\n\n"
            f"  [dim]The log didn't match any known error pattern.\n"
            f"  Try: [bold]opshero patterns sync[/bold] to update the pattern library.[/dim]\n",
            border_style="dim",
            padding=(0, 2),
        ))
        return

    # ── Match panel ─────────────────────────────────────────────────────────────
    border_col  = _confidence_color(confidence)
    offline_tag = "  [dim]· offline[/dim]" if is_offline else ""

    header = "\n".join([
        "",
        f"  [bold white]◉  {escape(pattern_id)}[/bold white]{offline_tag}",
        f"     [dim]category[/dim]  [white]{escape(category)}[/white]"
        f"  [dim]·[/dim]  [dim]engine[/dim]  {_method_badge(method)}",
        "",
        f"     [dim]confidence[/dim]  {_conf_bar(confidence)}",
        "",
    ])

    console.print()
    console.print(Panel(header, border_style=border_col, padding=(0, 1)))

    # ── Solutions ──────────────────────────────────────────────────────────────
    if solutions:
        console.print()
        n = len(solutions)
        for i, sol in enumerate(solutions, 1):
            rank  = sol.get("rank", i)
            title = sol.get("title", "")
            expl  = sol.get("explanation", "")
            cmd   = sol.get("command") or sol.get("command_template", "")
            risk  = sol.get("risk", "low")
            rev   = sol.get("reversible", True)
            conf  = sol.get("confidence", 0.0) or 0.0

            sc       = _confidence_color(conf)
            rev_text = f"[{_A}]reversible[/{_A}]" if rev else f"[{_B}]irreversible[/{_B}]"

            # Fix header line
            console.print(
                f"  [bold {_C}]▸ Fix {rank}/{n}[/bold {_C}]  [dim]—[/dim]  "
                f"[bold white]{escape(title)}[/bold white]  [{sc}]{conf:.0%}[/{sc}]"
            )
            console.rule(style="dim", characters="─")

            # Meta row
            console.print(
                f"  [dim]risk[/dim] {_risk_badge(risk)}"
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

    # ── Causal chain ───────────────────────────────────────────────────────────
    if causal:
        caused_by    = causal.get("caused_by") or []
        often_causes = causal.get("often_causes") or []
        if caused_by or often_causes:
            console.rule(f"[dim]causal chain[/dim]", style="dim", characters="─")
            if caused_by:
                parts = [f"[{_C}]{escape(p)}[/{_C}]" for p in caused_by]
                console.print("  [dim]caused by[/dim]   " + f"  [dim]→[/dim]  ".join(parts))
            if often_causes:
                parts = [f"[{_B}]{escape(p)}[/{_B}]" for p in often_causes]
                console.print("  [dim]leads to[/dim]    " + f"  [dim]→[/dim]  ".join(parts))
            console.print()

    # ── Footer ─────────────────────────────────────────────────────────────────
    footer = [f"[dim]⏱  {total_ms}ms[/dim]"]
    if llm_model:
        footer.append(f"[dim]◈  {escape(llm_model)}  {llm_ms}ms[/dim]")
    console.print("  " + "   ".join(footer))
    console.print()


# ── Feedback prompt ────────────────────────────────────────────────────────────

async def _prompt_feedback(cfg: Config, analysis_id: str) -> None:
    console.print()
    console.rule(f"[dim]feedback[/dim]", style="dim", characters="─")

    try:
        response = console.input(
            f"  [dim]Was this helpful? [[bold {_A}]y[/bold {_A}]/[bold {_D}]n[/bold {_D}]/skip]: [/dim]"
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if response in ("y", "yes"):
        helpful = True
    elif response in ("n", "no"):
        helpful = False
    else:
        return

    async with OpsHeroClient(cfg) as client:
        try:
            await client.submit_feedback(analysis_id, helpful)
            icon = f"[{_A}]✓[/{_A}]" if helpful else f"[{_D}]✗[/{_D}]"
            console.print(f"  {icon} [dim]Feedback recorded. Thanks![/dim]")
        except Exception:
            pass
