"""
Apply command — execute a fix suggestion from a previous analysis.

Usage:
  opshero apply                     # use last analysis, interactive pick
  opshero apply <analysis-id>       # specify analysis by ID or prefix
  opshero apply <id> --fix 1        # directly apply fix #1
  opshero apply <id> --dry-run      # print command without running it
  opshero apply <id> --fix 1 --yes  # skip confirmation prompt
"""

import asyncio
import platform
import shutil
import subprocess
import sys
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from opshero.api import APIError, AuthError, OpsHeroClient
from opshero.config import Config

console = Console()
err_console = Console(stderr=True)

# ── Brand palette ──────────────────────────────────────────────────────────────
_C = "#00d4ff"
_A = "#00ff87"
_B = "#ffb020"
_D = "#ff4444"


# ── OS detection ───────────────────────────────────────────────────────────────

def _get_os() -> str:
    """Return 'windows', 'macos', or 'linux'."""
    s = platform.system().lower()
    if s == "windows":
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def _is_windows() -> bool:
    return _get_os() == "windows"


# ── Command availability check ─────────────────────────────────────────────────

# Maps a tool name to install instructions per OS
_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "docker": {
        "windows": "Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/",
        "macos":   "Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/",
        "linux":   "sudo apt install docker.io  OR  https://docs.docker.com/engine/install/",
    },
    "kubectl": {
        "windows": "winget install Kubernetes.kubectl  OR  choco install kubernetes-cli",
        "macos":   "brew install kubectl",
        "linux":   "sudo apt install kubectl  OR  https://kubernetes.io/docs/tasks/tools/",
    },
    "helm": {
        "windows": "choco install kubernetes-helm  OR  winget install Helm.Helm",
        "macos":   "brew install helm",
        "linux":   "sudo snap install helm --classic  OR  https://helm.sh/docs/intro/install/",
    },
    "terraform": {
        "windows": "choco install terraform  OR  winget install Hashicorp.Terraform",
        "macos":   "brew install terraform",
        "linux":   "sudo apt install terraform  OR  https://developer.hashicorp.com/terraform/install",
    },
    "npm": {
        "windows": "Install Node.js (includes npm): https://nodejs.org/en/download/",
        "macos":   "brew install node  OR  https://nodejs.org/en/download/",
        "linux":   "sudo apt install nodejs npm  OR  https://nodejs.org/en/download/",
    },
    "yarn": {
        "windows": "npm install -g yarn",
        "macos":   "npm install -g yarn  OR  brew install yarn",
        "linux":   "npm install -g yarn",
    },
    "pip": {
        "windows": "Install Python: https://www.python.org/downloads/",
        "macos":   "brew install python  OR  https://www.python.org/downloads/",
        "linux":   "sudo apt install python3-pip",
    },
    "python": {
        "windows": "Install Python: https://www.python.org/downloads/",
        "macos":   "brew install python",
        "linux":   "sudo apt install python3",
    },
    "python3": {
        "windows": "Install Python: https://www.python.org/downloads/",
        "macos":   "brew install python",
        "linux":   "sudo apt install python3",
    },
    "git": {
        "windows": "Install Git: https://git-scm.com/download/win",
        "macos":   "brew install git  OR  xcode-select --install",
        "linux":   "sudo apt install git",
    },
    "make": {
        "windows": "choco install make  OR  winget install GnuWin32.Make",
        "macos":   "xcode-select --install",
        "linux":   "sudo apt install make",
    },
    "curl": {
        "windows": "curl is built-in on Windows 10+. Or: choco install curl",
        "macos":   "brew install curl",
        "linux":   "sudo apt install curl",
    },
    "aws": {
        "windows": "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        "macos":   "brew install awscli  OR  https://aws.amazon.com/cli/",
        "linux":   "pip install awscli  OR  https://aws.amazon.com/cli/",
    },
    "gcloud": {
        "windows": "https://cloud.google.com/sdk/docs/install#windows",
        "macos":   "brew install --cask google-cloud-sdk",
        "linux":   "https://cloud.google.com/sdk/docs/install#linux",
    },
    "az": {
        "windows": "winget install Microsoft.AzureCLI",
        "macos":   "brew install azure-cli",
        "linux":   "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash",
    },
}


def _check_command(cmd_name: str) -> bool:
    """Return True if the command is available on PATH."""
    # PowerShell built-in cmdlets are always available on Windows
    _PS_BUILTINS = {
        "Remove-Item", "Copy-Item", "Move-Item", "New-Item",
        "Get-Content", "Get-Command", "Select-String",
        "Set-Location", "Get-ChildItem", "Write-Output",
    }
    if cmd_name in _PS_BUILTINS:
        return True
    return shutil.which(cmd_name) is not None


def _get_first_token(cmd: str) -> str:
    """Extract the first word (binary name) from a shell command."""
    # Handle common prefixes like 'sudo', 'env', etc.
    tokens = cmd.strip().split()
    skip = {"sudo", "env", "time", "nice", "nohup"}
    for token in tokens:
        if token not in skip and not token.startswith("-") and not token.startswith("$"):
            return token.split("/")[-1]  # strip path prefix
    return tokens[0] if tokens else ""


def _adapt_command_for_os(cmd: str, os_name: str) -> str:
    """
    Adapt a bash command for the current OS.
    Handles common cross-platform differences.
    """
    if os_name != "windows":
        return cmd  # Linux/macOS: use as-is

    # Windows adaptations
    adaptations = [
        # rm -rf → Remove-Item -Recurse -Force (PowerShell)
        ("rm -rf ",          "Remove-Item -Recurse -Force "),
        ("rm -r ",           "Remove-Item -Recurse "),
        ("rm ",              "Remove-Item "),
        # mkdir -p → New-Item -ItemType Directory -Force
        ("mkdir -p ",        "New-Item -ItemType Directory -Force -Path "),
        # cp -r → Copy-Item -Recurse
        ("cp -r ",           "Copy-Item -Recurse "),
        ("cp ",              "Copy-Item "),
        # mv → Move-Item
        ("mv ",              "Move-Item "),
        # cat → Get-Content
        ("cat ",             "Get-Content "),
        # export VAR=val → $env:VAR="val"
        # (handled separately below)
        # chmod → no-op on Windows
        ("chmod +x ",        "# chmod not needed on Windows: "),
        ("chmod 755 ",       "# chmod not needed on Windows: "),
        ("chmod 644 ",       "# chmod not needed on Windows: "),
        # touch → New-Item
        ("touch ",           "New-Item -ItemType File -Force -Path "),
        # which → Get-Command
        ("which ",           "Get-Command "),
        # grep → Select-String
        ("grep ",            "Select-String -Pattern "),
        # && → ; in PowerShell
        (" && ",             " ; "),
        # || → -or equivalent (simplified)
        (" || true",         ""),
    ]

    result = cmd
    for bash_pat, ps_pat in adaptations:
        result = result.replace(bash_pat, ps_pat)

    # Handle export VAR=value → $env:VAR="value"
    import re
    result = re.sub(
        r'\bexport\s+(\w+)=(.+)',
        lambda m: f'$env:{m.group(1)}="{m.group(2).strip()}"',
        result,
    )

    return result


def _missing_tool_message(tool: str, os_name: str) -> str:
    """Return a helpful install message for a missing tool."""
    hint = _INSTALL_HINTS.get(tool, {}).get(os_name)
    if hint:
        return f"[{_B}]'{tool}' not found.[/{_B}]  Install it:\n  [dim]{hint}[/dim]"
    return f"[{_B}]'{tool}' not found on PATH.[/{_B}]  Please install it to run this fix."


# ── Styles ─────────────────────────────────────────────────────────────────────

def _risk_style(risk: str) -> str:
    return {
        "none":   "[dim]none[/dim]",
        "low":    f"[{_A}]low[/{_A}]",
        "medium": f"[{_B}]medium[/{_B}]",
        "high":   f"[bold {_D}]HIGH[/bold {_D}]",
    }.get(risk, risk)


def _confidence_color(c: float) -> str:
    if c >= 0.8:  return _A
    if c >= 0.55: return _B
    return _D


def _render_solutions(solutions: list[dict], os_name: str) -> None:
    """Print a numbered table of solutions with OS compatibility info."""
    table = Table(
        show_header=True,
        header_style=f"bold {_C}",
        border_style="dim",
        expand=True,
    )
    table.add_column("#",          width=3,  justify="right")
    table.add_column("Fix",        min_width=22)
    table.add_column("Confidence", width=12, justify="right")
    table.add_column("Risk",       width=10)
    table.add_column("Tool",       width=12)
    table.add_column("Available",  width=10, justify="center")

    for sol in solutions:
        rank  = sol.get("rank", "?")
        title = sol.get("title", "")
        conf  = sol.get("confidence", 0.0) or 0.0
        risk  = sol.get("risk", "low")
        cmd   = sol.get("command") or sol.get("command_template", "")
        color = _confidence_color(conf)

        # Adapt first, then check the adapted tool
        adapted_cmd = _adapt_command_for_os(cmd, os_name) if cmd else ""
        tool = _get_first_token(adapted_cmd) if adapted_cmd else ""
        if tool:
            available = _check_command(tool)
            avail_str = f"[{_A}]✓[/{_A}]" if available else f"[{_D}]✗[/{_D}]"
        else:
            avail_str = "[dim]—[/dim]"

        table.add_row(
            str(rank),
            escape(title),
            f"[{color}]{conf:.0%}[/{color}]",
            _risk_style(risk),
            f"[dim]{escape(tool)}[/dim]" if tool else "[dim]—[/dim]",
            avail_str,
        )

    console.print(table)


# ── Main command ───────────────────────────────────────────────────────────────

@click.command("apply")
@click.argument("analysis_id", default="", required=False)
@click.option("--fix", "-f", type=int, default=None,
              help="Fix number to apply (skips interactive pick)")
@click.option("--dry-run", is_flag=True,
              help="Print command but do not execute")
@click.option("--yes", "-y", is_flag=True,
              help="Skip confirmation prompt")
@click.option("--adapt/--no-adapt", default=True,
              help="Auto-adapt command for current OS (default: on)")
def apply_cmd(
    analysis_id: str,
    fix: Optional[int],
    dry_run: bool,
    yes: bool,
    adapt: bool,
):
    """
    Apply a fix suggestion from a previous analysis.

    \b
    Automatically detects your OS (Windows/macOS/Linux) and adapts
    the command if needed. Checks that required tools are installed
    before running.

    \b
    Examples:
      opshero apply                    # pick from last analysis
      opshero apply 54a177d2           # use 8-char ID prefix
      opshero apply 54a177d2 --fix 1   # apply fix #1 directly
      opshero apply 54a177d2 --dry-run # preview without running
      opshero apply --no-adapt         # use raw command as-is
    """
    asyncio.run(_apply(analysis_id or "", fix, dry_run, yes, adapt))


async def _apply(
    analysis_id: str,
    fix_rank: Optional[int],
    dry_run: bool,
    yes: bool,
    adapt: bool,
) -> None:
    cfg    = Config.load()
    os_name = _get_os()

    if not cfg.is_authenticated:
        err_console.print(
            f"[{_D}]Not authenticated.[/{_D}] "
            f"Run [bold {_C}]opshero login[/bold {_C}] first."
        )
        raise SystemExit(1)

    async with OpsHeroClient(cfg) as client:
        # ── Resolve analysis ──────────────────────────────────────────────────
        result: dict
        if not analysis_id:
            try:
                data  = await client.list_analyses(per_page=1)
                items = data.get("items") or data.get("analyses") or []
            except APIError as e:
                err_console.print(f"[{_D}]API error:[/{_D}] {e}")
                raise SystemExit(1)

            if not items:
                err_console.print(
                    f"[{_B}]No analyses found.[/{_B}] "
                    f"Run [bold {_C}]opshero analyze[/bold {_C}] first."
                )
                raise SystemExit(1)

            try:
                result = await client.get_analysis(items[0]["id"])
            except APIError as e:
                err_console.print(f"[{_D}]API error:[/{_D}] {e}")
                raise SystemExit(1)
        else:
            try:
                result = await client.get_analysis(analysis_id)
            except APIError as e:
                if e.status_code == 404 and len(analysis_id) < 32:
                    try:
                        data    = await client.list_analyses(per_page=100)
                        items   = data.get("items") or data.get("analyses") or []
                        matches = [i for i in items if i.get("id", "").startswith(analysis_id)]
                        if not matches:
                            err_console.print(f"[{_D}]Analysis not found:[/{_D}] {analysis_id}")
                            raise SystemExit(1)
                        if len(matches) > 1:
                            err_console.print(
                                f"[{_B}]Ambiguous prefix — {len(matches)} matches. "
                                f"Use more characters.[/{_B}]"
                            )
                            raise SystemExit(1)
                        result = await client.get_analysis(matches[0]["id"])
                    except SystemExit:
                        raise
                    except Exception as inner:
                        err_console.print(f"[{_D}]Error:[/{_D}] {inner}")
                        raise SystemExit(1)
                else:
                    err_console.print(f"[{_D}]API error:[/{_D}] {e}")
                    raise SystemExit(1)

    # ── Validate solutions ────────────────────────────────────────────────────
    solutions: list[dict] = result.get("solutions") or []
    actionable = [s for s in solutions if s.get("command") or s.get("command_template")]

    if not actionable:
        err_console.print(
            f"[{_B}]No executable fixes available for this analysis.[/{_B}]\n"
            "[dim]This pattern only has descriptive suggestions.[/dim]"
        )
        raise SystemExit(0)

    pattern_id = result.get("pattern_id", "?")
    category   = result.get("detected_category", "?")
    aid        = result.get("id", "")[:8]

    # ── OS banner ─────────────────────────────────────────────────────────────
    os_icons = {"windows": "🪟 Windows", "macos": "🍎 macOS", "linux": "🐧 Linux"}
    os_label = os_icons.get(os_name, os_name)

    console.print()
    console.print(Panel(
        f"  [bold]Analysis[/bold]  [dim]{aid}[/dim]  "
        f"[dim]·[/dim]  [bold]Pattern[/bold]  [{_C}]{escape(pattern_id)}[/{_C}]  "
        f"[dim]·[/dim]  [bold]Category[/bold]  {escape(category)}\n"
        f"  [bold]Platform[/bold]  [dim]{os_label}[/dim]"
        + ("  [dim]·[/dim]  [dim]auto-adapt ON[/dim]" if adapt else ""),
        title=f"[bold {_A}]OpsHero — Apply Fix[/bold {_A}]",
        border_style=f"{_A} dim",
        padding=(0, 2),
    ))
    console.print()
    _render_solutions(actionable, os_name)
    console.print()

    # ── Select fix ────────────────────────────────────────────────────────────
    chosen: dict
    if fix_rank is not None:
        matched = [s for s in actionable if s.get("rank") == fix_rank]
        if not matched:
            ranks = [str(s.get("rank", "?")) for s in actionable]
            err_console.print(
                f"[{_D}]Fix #{fix_rank} not found.[/{_D}] "
                f"Available: {', '.join(ranks)}"
            )
            raise SystemExit(1)
        chosen = matched[0]
    else:
        ranks = [str(s.get("rank", "?")) for s in actionable]
        while True:
            try:
                pick = console.input(
                    f"  [dim]Apply fix [[bold]{', '.join(ranks)}[/bold]] "
                    f"(or q to quit): [/dim]"
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [dim]Cancelled.[/dim]")
                raise SystemExit(0)

            if pick in ("q", "quit", ""):
                console.print("  [dim]Cancelled.[/dim]")
                raise SystemExit(0)

            try:
                rank_int = int(pick)
                matched  = [s for s in actionable if s.get("rank") == rank_int]
                if matched:
                    chosen = matched[0]
                    break
                err_console.print(f"  [{_D}]No fix #{rank_int}.[/{_D}]")
            except ValueError:
                err_console.print(f"  [{_D}]Enter a number.[/{_D}]")

    # ── Prepare command ───────────────────────────────────────────────────────
    raw_cmd  = chosen.get("command") or chosen.get("command_template", "")
    risk     = chosen.get("risk", "low")
    title    = chosen.get("title", "")
    expl     = chosen.get("explanation", "")
    rev      = chosen.get("reversible", True)

    # Adapt command for OS FIRST, then check tool availability
    final_cmd = _adapt_command_for_os(raw_cmd, os_name) if adapt else raw_cmd
    was_adapted = adapt and final_cmd != raw_cmd

    # Check tool from the ADAPTED command (not the original)
    tool = _get_first_token(final_cmd)
    tool_available = _check_command(tool) if tool else True

    # ── Show fix detail ───────────────────────────────────────────────────────
    console.print(
        f"  [bold {_C}]Fix #{chosen.get('rank')}[/bold {_C}]  [dim]—[/dim]  "
        f"[bold white]{escape(title)}[/bold white]"
    )
    if expl:
        console.print(f"  [dim]{escape(expl)}[/dim]")
    console.print(
        f"  [dim]risk[/dim] {_risk_style(risk)}  "
        f"[dim]reversible[/dim] "
        + (f"[{_A}]yes[/{_A}]" if rev else f"[{_B}]no[/{_B}]")
    )
    console.print()

    # Show original vs adapted
    if was_adapted:
        console.print(f"  [dim]Original (bash):[/dim]")
        console.print(Syntax(raw_cmd,   "bash",       theme="monokai",
                             background_color="default", padding=(1, 4)))
        console.print(f"  [dim]Adapted for {os_label}:[/dim]")
        console.print(Syntax(final_cmd, "powershell" if _is_windows() else "bash",
                             theme="monokai", background_color="default", padding=(1, 4)))
    else:
        console.print(Syntax(final_cmd, "bash", theme="monokai",
                             background_color="default", padding=(1, 4)))

    console.print()

    # ── Tool availability warning ─────────────────────────────────────────────
    if not tool_available and tool:
        console.print(
            Panel(
                f"\n  {_missing_tool_message(tool, os_name)}\n",
                title=f"[bold {_B}]⚠  Missing Tool[/bold {_B}]",
                border_style=_B,
                padding=(0, 2),
            )
        )
        console.print()
        if not dry_run:
            try:
                proceed = console.input(
                    f"  [dim]'{tool}' is not installed. "
                    f"Continue anyway? [[bold]y[/bold]/N]: [/dim]"
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [dim]Cancelled.[/dim]")
                raise SystemExit(0)
            if proceed not in ("y", "yes"):
                console.print("  [dim]Aborted. Install the tool first, then re-run.[/dim]")
                raise SystemExit(0)

    if dry_run:
        console.print(f"  [dim]Dry-run mode — command not executed.[/dim]")
        raise SystemExit(0)

    # ── Confirmation ──────────────────────────────────────────────────────────
    if not yes:
        if risk == "high":
            console.print(
                f"  [bold {_D}]⚠  HIGH RISK[/bold {_D}] — "
                "This command may have significant side effects."
            )
            if not rev:
                console.print(f"  [{_B}]This action is irreversible.[/{_B}]")
            console.print()

        try:
            confirm = console.input(
                f"  [dim]Run this command? [[bold {_A}]y[/bold {_A}]/N]: [/dim]"
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]Cancelled.[/dim]")
            raise SystemExit(0)

        if confirm not in ("y", "yes"):
            console.print("  [dim]Aborted.[/dim]")
            raise SystemExit(0)

    # ── Execute ───────────────────────────────────────────────────────────────
    console.print(f"  [dim]Running…[/dim]\n")

    # On Windows use PowerShell for adapted commands, cmd.exe for raw
    if _is_windows() and was_adapted:
        shell_cmd = ["powershell", "-NoProfile", "-Command", final_cmd]
        use_shell = False
    else:
        shell_cmd = final_cmd
        use_shell = True

    try:
        proc = subprocess.run(shell_cmd, shell=use_shell, text=True)
        if proc.returncode == 0:
            console.print(f"\n  [{_A}]✓[/{_A}] Command completed successfully.")
        else:
            console.print(
                f"\n  [{_D}]✗[/{_D}] Command exited with code {proc.returncode}."
            )
            raise SystemExit(proc.returncode)
    except FileNotFoundError:
        err_console.print(
            f"\n  [{_D}]Command not found:[/{_D}] '{_get_first_token(final_cmd)}'\n"
            f"  {_missing_tool_message(_get_first_token(final_cmd), os_name)}"
        )
        raise SystemExit(1)
    except PermissionError:
        err_console.print(
            f"\n  [{_D}]Permission denied.[/{_D}] "
            + ("Try running as Administrator." if _is_windows()
               else "Try adding 'sudo' before the command.")
        )
        raise SystemExit(1)
