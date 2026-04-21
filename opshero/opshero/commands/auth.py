"""
Auth commands — login, logout, whoami.

opshero login    — GitHub Device Flow (opens browser, polls API)
opshero logout   — blacklist token on server, clear local config
opshero whoami   — show current user info
"""

import asyncio
import time
import webbrowser

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from opshero.api import APIError, AuthError, OpsHeroClient, RateLimitError
from opshero.config import Config

console = Console()

# ── Brand palette ──────────────────────────────────────────────────────────────
_C = "#00d4ff"    # --cyan
_A = "#00ff87"    # --acid
_B = "#ffb020"    # --amber
_D = "#ff4444"    # --danger

_TIER_COLOR = {
    "free":  _C,
    "pro":   _A,
    "team":  _B,
}


# ── login ──────────────────────────────────────────────────────────────────────

@click.command("login")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
def login_cmd(no_browser: bool):
    """Authenticate with GitHub (Device Flow — works in SSH / CI too)."""
    cfg = Config.load()
    asyncio.run(_login(cfg, no_browser))


async def _login(cfg: Config, no_browser: bool) -> None:
    async with OpsHeroClient(cfg) as client:
        with console.status(f"[{_C}]Contacting GitHub…[/{_C}]"):
            try:
                data = await client.device_code()
            except APIError as e:
                console.print(f"  [{_D}]Error:[/{_D}] {e}")
                raise SystemExit(1)

        user_code        = data["user_code"]
        verification_uri = data["verification_uri"]
        device_code      = data["device_code"]
        interval         = data.get("interval", 5)
        expires_in       = data.get("expires_in", 900)

        # ── Instructions panel ────────────────────────────────────────────────
        instructions = Text()
        instructions.append("\n  1  ", style="bold dim")
        instructions.append("Open  ", style="dim")
        instructions.append(verification_uri, style=f"bold {_C} underline")
        instructions.append("\n\n  2  ", style="bold dim")
        instructions.append("Enter code  ", style="dim")
        instructions.append(user_code, style=f"bold {_B}")
        instructions.append("\n\n  ", style="dim")
        instructions.append("Waiting for GitHub authorization…", style="dim italic")
        instructions.append("\n")

        console.print()
        console.print(Panel(
            instructions,
            title=f"[bold {_C}]OpsHero Login[/bold {_C}]",
            border_style=f"{_C} dim",
            padding=(0, 2),
            expand=False,
        ))

        if not no_browser:
            try:
                webbrowser.open(verification_uri)
                console.print(f"  [dim]Browser opened automatically.[/dim]")
            except Exception:
                pass

        # ── Poll ──────────────────────────────────────────────────────────────
        deadline = time.monotonic() + expires_in
        with console.status(f"[{_C}]Waiting for authorization…[/{_C}]") as status:
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                try:
                    token_data = await client.device_poll(device_code)
                except APIError as e:
                    if e.status_code == 202:
                        continue
                    if e.status_code == 429:
                        interval += 5
                        continue
                    if e.status_code == 410:
                        console.print(
                            f"  [{_D}]Device code expired.[/{_D}] "
                            f"Run [bold {_C}]opshero login[/bold {_C}] again."
                        )
                        raise SystemExit(1)
                    console.print(f"  [{_D}]Login failed:[/{_D}] {e}")
                    raise SystemExit(1)
                else:
                    cfg.access_token  = token_data["access_token"]
                    cfg.refresh_token = token_data["refresh_token"]
                    cfg.token_expires_at = None

                    status.update(f"[{_C}]Fetching profile…[/{_C}]")
                    try:
                        me = await client.get_me()
                        cfg.github_login      = me.get("github_login")
                        cfg.github_avatar_url = me.get("github_avatar_url")
                        cfg.user_tier         = me.get("tier", "free")
                    except Exception:
                        pass

                    cfg.save()
                    tier_col = _TIER_COLOR.get(cfg.user_tier or "free", _C)
                    console.print(
                        f"\n  [{_A}]✓[/{_A}] [bold]Logged in[/bold]  "
                        f"[{_C}]@{cfg.github_login or 'unknown'}[/{_C}]  "
                        f"[dim]·[/dim]  [{tier_col}]{cfg.user_tier}[/{tier_col}]"
                    )
                    return

        console.print(f"  [{_D}]Timed out waiting for authorization.[/{_D}]")
        raise SystemExit(1)


# ── logout ─────────────────────────────────────────────────────────────────────

@click.command("logout")
def logout_cmd():
    """Log out and invalidate your session token."""
    cfg = Config.load()
    if not cfg.is_authenticated:
        console.print(f"  [{_B}]You are not logged in.[/{_B}]")
        return

    asyncio.run(_logout(cfg))


async def _logout(cfg: Config) -> None:
    async with OpsHeroClient(cfg) as client:
        await client.logout()

    login = cfg.github_login or "unknown"
    cfg.clear_auth()
    cfg.save()
    console.print(f"  [{_A}]✓[/{_A}] Logged out  [dim]@{login}[/dim]")


# ── whoami ─────────────────────────────────────────────────────────────────────

@click.command("whoami")
def whoami_cmd():
    """Show the currently authenticated user."""
    cfg = Config.load()
    if not cfg.is_authenticated:
        console.print(
            f"  [{_B}]Not logged in.[/{_B}]  "
            f"Run [bold {_C}]opshero login[/bold {_C}]"
        )
        return

    asyncio.run(_whoami(cfg))


async def _whoami(cfg: Config) -> None:
    async with OpsHeroClient(cfg) as client:
        try:
            me = await client.get_me()
        except AuthError:
            console.print(
                f"  [{_D}]Session expired.[/{_D}]  "
                f"Run [bold {_C}]opshero login[/bold {_C}]"
            )
            raise SystemExit(1)

    tier      = me.get("tier", "free")
    tier_col  = _TIER_COLOR.get(tier, _C)
    login     = me.get("github_login", "?")
    name      = me.get("github_name") or "—"

    console.print()

    tbl = Table(show_header=False, box=None, padding=(0, 2, 0, 2), expand=False)
    tbl.add_column("key", style="dim", width=12)
    tbl.add_column("val")

    tbl.add_row("Login",     f"[bold {_C}]@{login}[/bold {_C}]")
    tbl.add_row("Name",      f"[white]{name}[/white]")
    tbl.add_row("Tier",      f"[bold {tier_col}]{tier}[/bold {tier_col}]")
    tbl.add_row("API",       f"[dim]{cfg.api_url}[/dim]")
    tbl.add_row("Client ID", f"[dim]{cfg.client_id[:8]}…[/dim]")

    console.print(tbl)
    console.print()
