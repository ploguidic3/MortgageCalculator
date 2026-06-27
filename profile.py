"""Rolling player profile: baselines, recurring findings, and trend context.

All logic here is deterministic (no LLM) so it is fully testable offline.
The profile is computed from the matches stored in coach.db and is fed into
the report prompt so feedback can reference the player's history.
"""
from collections import Counter

# Metrics we track baselines for, and whether higher or lower is better.
METRIC_DIRECTION = {
    "deaths": "lower",
    "kda": "higher",
    "gpm": "higher",
    "xpm": "higher",
    "last_hits": "higher",
    "lh_per_min": "higher",
    "cs_at_10": "higher",
    "hero_damage": "higher",
    "tower_damage": "higher",
    "teamfight_participation": "higher",
    "obs_placed": "higher",
    "sen_placed": "higher",
}

# Finding rules: (key, metric, scope, label, trigger ratio vs baseline).
# scope: "all" = any role, "core" = P1-P3, "support" = P4-P5.
FINDING_DEFS = [
    ("high_deaths", "deaths", "all", "High deaths", 1.25),
    ("low_kda", "kda", "all", "Low KDA", 0.75),
    ("low_xpm", "xpm", "all", "Low XPM", 0.80),
    ("low_teamfight", "teamfight_participation", "all", "Low teamfight participation", 0.75),
    ("low_cs10", "cs_at_10", "core", "Low CS@10", 0.80),
    ("low_gpm", "gpm", "core", "Low GPM", 0.80),
    ("low_lasthits", "last_hits", "core", "Low last hits", 0.80),
    ("low_obs", "obs_placed", "support", "Few observer wards", 0.70),
    ("low_sen", "sen_placed", "support", "Few sentry wards", 0.70),
]

# Minimum prior games before we trust a baseline enough to flag findings.
MIN_HISTORY = 3
# Window sizes.
BASELINE_WINDOW = 10
RECENT_WINDOW = 5


def _is_core(position) -> bool:
    return position in (1, 2, 3)


def _is_support(position) -> bool:
    return position in (4, 5)


def _scope_applies(scope: str, position) -> bool:
    if scope == "all":
        return True
    if scope == "core":
        return _is_core(position)
    if scope == "support":
        return _is_support(position)
    return False


def _avg(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def compute_profile(matches: list, position=None) -> dict:
    """Build baselines from history.

    If `position` is given and there are enough same-role games, baselines are
    role-specific; otherwise they fall back to all games. Most recent games
    (by list order — callers pass oldest-first) are weighted by windowing.
    """
    role_matches = [m for m in matches if m.get("position") == position] if position else []
    if position is not None and len(role_matches) >= MIN_HISTORY:
        scope_matches = role_matches
        scope = "role"
    else:
        scope_matches = matches
        scope = "overall"

    window = scope_matches[-BASELINE_WINDOW:]
    baseline = {
        metric: _avg([m.get(metric) for m in window])
        for metric in METRIC_DIRECTION
    }

    return {
        "baseline": baseline,
        "baseline_scope": scope,
        "n_total": len(matches),
        "n_role": len(role_matches),
        "n_window": len(window),
        "position": position,
    }


def detect_findings(metrics: dict, profile: dict) -> list:
    """Compare a single match's metrics against the baseline and flag issues."""
    if profile["n_window"] < MIN_HISTORY:
        return []

    baseline = profile["baseline"]
    position = metrics.get("position")
    findings = []

    for key, metric, scope, label, ratio in FINDING_DEFS:
        if not _scope_applies(scope, position):
            continue
        value = metrics.get(metric)
        base = baseline.get(metric)
        if value is None or base is None or base <= 0:
            continue

        direction = METRIC_DIRECTION[metric]
        triggered = False
        if direction == "lower":
            # e.g. deaths: flag when well above baseline (and not a trivial +1)
            triggered = value > base * ratio and (value - base) >= 2
        else:
            triggered = value < base * ratio

        if triggered:
            findings.append({
                "key": key,
                "label": label,
                "metric": metric,
                "value": value,
                "baseline": round(base, 1),
            })
    return findings


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def build_trend_context(metrics: dict, profile: dict, current_findings: list,
                        recent_findings: list) -> str | None:
    """Render a history/trend block for the prompt, or None if too little data."""
    if profile["n_total"] < MIN_HISTORY:
        return None

    baseline = profile["baseline"]
    position = metrics.get("position")
    scope_label = (f"as P{position}" if profile["baseline_scope"] == "role" and position
                   else "across all roles")

    lines = [
        f"PLAYER HISTORY — baselines from your last {profile['n_window']} games "
        f"({scope_label}); {profile['n_total']} games tracked total:",
    ]

    # Show the headline metrics this game vs the player's own average.
    headline = ["deaths", "kda", "gpm", "xpm"]
    if _is_core(position):
        headline += ["cs_at_10", "last_hits"]
    elif _is_support(position):
        headline += ["obs_placed", "sen_placed"]

    for metric in headline:
        base = baseline.get(metric)
        value = metrics.get(metric)
        if base is None or value is None:
            continue
        direction = METRIC_DIRECTION[metric]
        if direction == "lower":
            verdict = "better" if value < base else ("worse" if value > base else "on par")
        else:
            verdict = "better" if value > base else ("worse" if value < base else "on par")
        lines.append(
            f"- {metric}: this game {_fmt(value)} vs your avg {_fmt(base)} ({verdict})"
        )

    # Recurring issues across recent games.
    counts = Counter(f["finding_key"] for f in recent_findings)
    labels = {f["finding_key"]: f["label"] for f in recent_findings}
    recurring = [(k, c) for k, c in counts.items() if c >= 2]
    current_keys = {f["key"] for f in current_findings}

    if recurring:
        lines.append("")
        lines.append(f"RECURRING ISSUES (in your last {RECENT_WINDOW} processed games):")
        for key, count in sorted(recurring, key=lambda kc: -kc[1]):
            label = labels.get(key, key)
            still = "still present this game" if key in current_keys else "NOT flagged this game — improvement"
            lines.append(f"- {label}: flagged in {count} recent games; {still}.")

    if current_findings:
        lines.append("")
        lines.append("FLAGGED THIS GAME (metric below your baseline):")
        for f in current_findings:
            lines.append(
                f"- {f['label']}: {_fmt(f['value'])} vs baseline {_fmt(f['baseline'])}."
            )

    return "\n".join(lines)
