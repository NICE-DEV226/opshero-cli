"""
Offline analysis engine for the CLI.

Used when:
  - User is not authenticated
  - Network is unavailable
  - --offline flag is passed

Runs the same regex pipeline as the backend but locally,
using the patterns cached in ~/.opshero/patterns_cache.json.
Returns a dict compatible with the API's AnalyzeResponse schema.
"""

import hashlib
import re
import time
from typing import Optional


# ── Preprocessing (minimal, mirrors backend) ───────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_MAX_CHARS = 6000


def _clean_log(raw: str) -> str:
    cleaned = _ANSI_RE.sub("", raw)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    if len(cleaned) > _MAX_CHARS:
        cleaned = cleaned[-_MAX_CHARS:]  # keep end (errors are at the bottom)
    return cleaned


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text.lower()))
    return tokens


_CATEGORY_SIGNALS: dict[str, list[str]] = {
    "docker": ["dockerfile", "docker", "container", "image", "layer", "runc"],
    "npm": ["npm", "node_modules", "package.json", "yarn", "pnpm"],
    "python": ["pip", "python", "traceback", "importerror", "modulenotfounderror"],
    "git": ["git", "remote", "branch", "merge", "commit", "push", "pull"],
    "tests": ["pytest", "jest", "assert", "testcase", "coverage", "failed test"],
}


def _detect_category(tokens: set[str], log_lower: str) -> Optional[str]:
    scores: dict[str, int] = {}
    for cat, signals in _CATEGORY_SIGNALS.items():
        score = sum(1 for s in signals if s in log_lower)
        if score:
            scores[cat] = score
    if not scores:
        return None
    return max(scores, key=lambda k: scores[k])


# ── Pattern testing ────────────────────────────────────────────────────────────

def _apply_transform(value: str, transform: str) -> str:
    match transform:
        case "strip":     return value.strip()
        case "lowercase": return value.strip().lower()
        case "uppercase": return value.strip().upper()
        case "basename":
            v = value.strip()
            return v.split("/")[-1].split("\\")[-1]
        case _:           return value


def _extract_variables(var_defs: dict, m: re.Match) -> dict:
    result: dict = {}
    groups = m.groups()
    for var_name, var_def in var_defs.items():
        source = var_def.get("from", "regex_group_1")
        default = var_def.get("default", "")
        transform = var_def.get("transform", "strip")
        try:
            if source.startswith("regex_group_"):
                idx = int(source.split("_")[-1]) - 1
                raw = groups[idx] if idx < len(groups) else default
            elif source == "named_group":
                raw = m.group(var_name) or default
            else:
                raw = default
            result[var_name] = _apply_transform(raw or default, transform)
        except (IndexError, AttributeError):
            result[var_name] = default
    return result


def _interpolate(template: str, variables: dict) -> str:
    """Replace {var} placeholders; leave [var?] for missing ones."""
    result = template
    for key, val in variables.items():
        result = result.replace(f"{{{key}}}", val)
    return result


def _test_pattern(pattern: dict, cleaned_log: str, log_lower: str) -> Optional[dict]:
    detection = pattern.get("detection", {})

    for exc in detection.get("exclude_if", []):
        if exc.lower() in log_lower:
            return None

    for kw in detection.get("keywords_required", []):
        if kw.lower() not in log_lower:
            return None

    variables: dict = {}
    regex_matched = False
    regex_str = detection.get("regex")

    if regex_str:
        try:
            compiled = re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
            m = compiled.search(cleaned_log)
            if m:
                regex_matched = True
                variables = _extract_variables(detection.get("variables", {}), m)
        except re.error:
            return None

    # Simple confidence: base 0.3 + regex bonus + optional kws
    confidence = 0.30
    if regex_matched:
        confidence += 0.35
    opt_found = sum(
        1 for kw in detection.get("keywords_optional", []) if kw.lower() in log_lower
    )
    confidence += min(opt_found * 0.06, 0.20)
    if variables:
        confidence += 0.05
    confidence = min(confidence, 0.95)

    if confidence < 0.30:
        return None

    return {
        "pattern_id": pattern["pattern_id"],
        "confidence": round(confidence, 3),
        "regex_matched": regex_matched,
        "variables": variables,
        "pattern": pattern,
    }


def _generate_solutions(pattern: dict, variables: dict) -> list[dict]:
    solutions = []
    for s in pattern.get("solutions", []):
        cmd = s.get("command_template", "")
        if cmd:
            cmd = _interpolate(cmd, variables)
        solutions.append({
            "rank": s.get("rank", 1),
            "title": s.get("title", ""),
            "explanation": _interpolate(s.get("explanation", ""), variables),
            "confidence": s.get("confidence", 0.7),
            "risk": s.get("risk", "low"),
            "reversible": s.get("reversible", True),
            "requires_confirmation": s.get("requires_confirmation", False),
            "command": cmd,
        })
    return sorted(solutions, key=lambda x: x["rank"])


# ── Public API ─────────────────────────────────────────────────────────────────

def analyze_offline(raw_log: str, patterns: list[dict]) -> dict:
    """
    Run the offline regex engine against cached patterns.
    Returns a dict mirroring AnalyzeResponse (no id, no LLM fields).
    """
    start = time.monotonic()

    cleaned = _clean_log(raw_log)
    log_lower = cleaned.lower()
    tokens = _tokenize(cleaned)
    category = _detect_category(tokens, log_lower)
    log_hash = hashlib.sha256(raw_log.encode()).hexdigest()[:16]

    # Score all patterns (no inverted index in offline mode — brute force, small N)
    candidates: list[dict] = []
    for p in patterns:
        m = _test_pattern(p, cleaned, log_lower)
        if m:
            candidates.append(m)

    best = max(candidates, key=lambda c: c["confidence"]) if candidates else None

    solutions: list[dict] = []
    if best:
        solutions = _generate_solutions(best["pattern"], best["variables"])

    latency_ms = int((time.monotonic() - start) * 1000)

    return {
        "id": None,
        "pattern_id": best["pattern_id"] if best else None,
        "confidence": best["confidence"] if best else 0.0,
        "match_method": "regex_offline" if best else "no_match",
        "detected_category": category,
        "extracted_vars": best["variables"] if best else {},
        "solutions": solutions,
        "causal_chain": None,
        "llm_model": None,
        "llm_latency_ms": None,
        "total_latency_ms": latency_ms,
        "error": None,
        "log_hash": log_hash,
        "_offline": True,
    }
