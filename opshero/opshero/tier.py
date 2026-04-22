"""
Tier-based feature gating for the OpsHero CLI.

The backend enforces limits server-side. This module provides
client-side checks to give users clear, actionable messages
BEFORE making API calls that would fail.

Tier hierarchy: free < pro < team
"""

from typing import Optional
from rich.console import Console
from rich.panel import Panel

console = Console()

_C = "#00d4ff"
_A = "#00ff87"
_B = "#ffb020"
_D = "#ff4444"

# ── Tier definitions ───────────────────────────────────────────────────────────

TIER_HIERARCHY = {"free": 0, "pro": 1, "team": 2, "enterprise": 3}

TIER_FEATURES = {
    "free": {
        "analyses_per_day":  10,
        "history_days":       7,
        "llm_enabled":    False,
        "sync_enabled":   False,
        "team_enabled":   False,
        "offline_enabled": True,
        "rerun_enabled":   True,
        "apply_enabled":   True,
        "contribute_enabled": True,
    },
    "pro": {
        "analyses_per_day":  500,
        "history_days":       90,
        "llm_enabled":     True,
        "sync_enabled":    True,
        "team_enabled":   False,
        "offline_enabled": True,
        "rerun_enabled":   True,
        "apply_enabled":   True,
        "contribute_enabled": True,
    },
    "team": {
        "analyses_per_day":  -1,   # unlimited
        "history_days":      365,
        "llm_enabled":     True,
        "sync_enabled":    True,
        "team_enabled":    True,
        "offline_enabled": True,
        "rerun_enabled":   True,
        "apply_enabled":   True,
        "contribute_enabled": True,
    },
    "enterprise": {
        "analyses_per_day":  -1,
        "history_days":      -1,
        "llm_enabled":     True,
        "sync_enabled":    True,
        "team_enabled":    True,
        "offline_enabled": True,
        "rerun_enabled":   True,
        "apply_enabled":   True,
        "contribute_enabled": True,
    },
}

# ── Upgrade messages ───────────────────────────────────────────────────────────

UPGRADE_MESSAGES = {
    "sync_enabled": (
        "pro", "team",
        "Cloud sync requires a [bold]Pro[/bold] or [bold]Team[/bold] plan.\n"
        "  Sync lets you push/pull analyses across machines and teammates.",
    ),
    "team_enabled": (
        "team",
        None,
        "Team features require a [bold]Team[/bold] plan.\n"
        "  Manage projects, share analyses, and collaborate with your team.",
    ),
    "llm_enabled": (
        "pro", "team",
        "AI engine fallback requires a [bold]Pro[/bold] or [bold]Team[/bold] plan.\n"
        "  The AI engine handles unknown errors that regex patterns can't match.",
    ),
}


# ── Gate functions ─────────────────────────────────────────────────────────────

def get_tier_features(tier: str) -> dict:
    """Return feature set for a given tier."""
    return TIER_FEATURES.get(tier, TIER_FEATURES["free"])


def has_feature(tier: str, feature: str) -> bool:
    """Check if a tier has a specific feature enabled."""
    features = get_tier_features(tier)
    return bool(features.get(feature, False))


def require_feature(tier: str, feature: str, exit_on_fail: bool = True) -> bool:
    """
    Check if tier has feature. If not, print a clear upgrade message.
    Returns True if allowed, False (or exits) if not.
    """
    if has_feature(tier, feature):
        return True

    # Build upgrade message
    msg_data = UPGRADE_MESSAGES.get(feature)
    if msg_data:
        required_tiers = msg_data[:-1]  # all but last element
        description = msg_data[-1]
        tier_labels = " or ".join(f"[bold {_B}]{t.capitalize()}[/bold {_B}]" for t in required_tiers if t)
        upgrade_line = f"Upgrade to {tier_labels} to unlock this feature."
    else:
        upgrade_line = f"This feature is not available on the [bold]{tier}[/bold] plan."
        description = ""

    body = ""
    if description:
        body += f"\n  {description}\n"
    body += f"\n  {upgrade_line}"
    body += f"\n\n  [{_C}]https://opshero.me/dashboard/upgrade[/{_C}]\n"

    console.print()
    console.print(Panel(
        body,
        title=f"[bold {_B}]Feature not available on {tier.capitalize()} plan[/bold {_B}]",
        border_style=_B,
        padding=(0, 2),
    ))

    if exit_on_fail:
        raise SystemExit(0)
    return False


def warn_analyses_limit(tier: str, used: int) -> None:
    """Warn user when approaching their daily analysis limit."""
    features = get_tier_features(tier)
    limit = features.get("analyses_per_day", 10)
    if limit == -1:
        return  # unlimited

    pct = used / limit if limit > 0 else 1.0

    if pct >= 1.0:
        console.print(
            f"\n  [{_D}]Daily limit reached[/{_D}]  "
            f"[dim]{used}/{limit} analyses used today.[/dim]\n"
            f"  Upgrade for more: [{_C}]https://opshero.me/dashboard/upgrade[/{_C}]\n"
        )
    elif pct >= 0.8:
        console.print(
            f"  [{_B}]⚠  {used}/{limit} analyses used today ({pct:.0%})[/{_B}]  "
            f"[dim]Upgrade for more.[/dim]"
        )


def tier_label(tier: str) -> str:
    """Return a colored tier label for display."""
    colors = {"free": _C, "pro": _A, "team": _B, "enterprise": "#a78bfa"}
    color = colors.get(tier, _C)
    return f"[bold {color}]{tier.capitalize()}[/bold {color}]"
