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

# Metrics whose meaning depends heavily on role — a P1 carry and a P5 support
# have wildly different GPM, farm, and ward numbers. These are ONLY ever
# compared against same-role games; we never mix roles for them. Everything
# else (deaths, KDA, teamfight %) may fall back to an all-roles baseline.
ROLE_SENSITIVE = {
    "gpm", "xpm", "last_hits", "lh_per_min", "cs_at_10",
    "hero_damage", "tower_damage", "obs_placed", "sen_placed",
}

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
    """Build per-metric baselines from history.

    Each metric's baseline is computed independently:
      * Role-sensitive metrics (GPM, farm, wards, ...) use ONLY same-role games
        and have no baseline until there are enough of them — they are never
        averaged across roles.
      * Role-neutral metrics (deaths, KDA, teamfight %) prefer a same-role
        baseline but fall back to an all-roles baseline when role history is thin.
    Callers pass matches oldest-first; the most recent BASELINE_WINDOW are used.
    """
    role_matches = [m for m in matches if m.get("position") == position] if position is not None else []
    role_window = role_matches[-BASELINE_WINDOW:]
    overall_window = matches[-BASELINE_WINDOW:]

    baseline = {}
    source = {}
    for metric in METRIC_DIRECTION:
        role_avg = _avg([m.get(metric) for m in role_window]) if len(role_window) >= MIN_HISTORY else None
        if metric in ROLE_SENSITIVE:
            baseline[metric] = role_avg
            source[metric] = "role" if role_avg is not None else None
        elif role_avg is not None:
            baseline[metric] = role_avg
            source[metric] = "role"
        else:
            overall_avg = _avg([m.get(metric) for m in overall_window]) if len(overall_window) >= MIN_HISTORY else None
            baseline[metric] = overall_avg
            source[metric] = "overall" if overall_avg is not None else None

    return {
        "baseline": baseline,
        "baseline_source": source,
        "n_total": len(matches),
        "n_role": len(role_matches),
        "n_role_window": len(role_window),
        "n_window": len(overall_window),
        "position": position,
    }


def detect_findings(metrics: dict, profile: dict) -> list:
    """Compare a single match's metrics against the baseline and flag issues.

    A finding only fires when that metric actually has a baseline, so
    role-sensitive metrics are never flagged without same-role history.
    """
    if profile["n_total"] < MIN_HISTORY:
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
    source = profile["baseline_source"]
    position = metrics.get("position")
    role_note = f", {profile['n_role']} as P{position}" if position else ""

    lines = [
        f"PLAYER HISTORY — {profile['n_total']} games tracked{role_note}.",
        "Role-sensitive stats (GPM, XPM, CS@10, last hits, wards) are compared "
        "ONLY against your games in the same role; role-neutral stats (deaths, "
        "KDA, teamfight %) may use all roles.",
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
        scope_txt = f"P{position} avg" if source.get(metric) == "role" and position else "overall avg"
        direction = METRIC_DIRECTION[metric]
        if direction == "lower":
            verdict = "better" if value < base else ("worse" if value > base else "on par")
        else:
            verdict = "better" if value > base else ("worse" if value < base else "on par")
        lines.append(
            f"- {metric}: this game {_fmt(value)} vs your {scope_txt} {_fmt(base)} ({verdict})"
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
