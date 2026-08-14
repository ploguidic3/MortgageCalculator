#!/usr/bin/env python3
import argparse
import os
import sys
import time
import json
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
import anthropic

import db
import profile as player_profile

try:
    load_dotenv()
except UnicodeDecodeError:
    # .env was saved as UTF-16 (Windows Notepad default) — retry with correct encoding
    load_dotenv(encoding="utf-16")

ACCOUNT_ID = int(os.getenv("DOTA_ACCOUNT_ID", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENDOTA_BASE = "https://api.opendota.com/api"
DB_PATH = os.getenv("COACH_DB", "coach.db")

HERO_NAMES = {}  # populated lazily

LANE_NAMES = {
    0: "Unknown",
    1: "Safe Lane",
    2: "Mid Lane",
    3: "Off Lane",
    4: "Jungle",
}


def infer_position(match_data: dict, player: dict, is_radiant: bool) -> int:
    """Determine P1-P5 using lane + net worth.

    Position number is tied to lane, not just farm:
      P1 carry / P5 hard support  -> safe lane (P1 richer, P5 poorer)
      P2 mid                      -> mid lane
      P3 offlane / P4 soft support-> off lane (P3 richer, P4 poorer)
    Falls back to pure net-worth rank when lane data is missing/ambiguous.
    """
    team = [
        p for p in match_data.get("players", [])
        if (p.get("isRadiant", p.get("player_slot", 0) < 128)) == is_radiant
    ]

    def nw(p):
        return p.get("net_worth", p.get("total_gold", 0))

    slot = player.get("player_slot")
    lane = player.get("lane")
    roaming = player.get("is_roaming", False)

    # Lane-aware assignment (lane: 1=safe, 2=mid, 3=off, normalized per team)
    if not roaming and lane in (1, 2, 3):
        same_lane = sorted(
            [p for p in team if p.get("lane") == lane and not p.get("is_roaming", False)],
            key=nw, reverse=True,
        )
        idx = next((i for i, p in enumerate(same_lane) if p.get("player_slot") == slot), 0)
        if lane == 2:
            return 2
        if lane == 1:
            return 1 if idx == 0 else 5
        if lane == 3:
            return 3 if idx == 0 else 4

    # Fallback: rank by net worth descending within the team
    by_nw = sorted(team, key=nw, reverse=True)
    for i, p in enumerate(by_nw):
        if p.get("player_slot") == slot:
            return i + 1
    return 0


def http_get(url: str, params: dict | None = None, timeout: int = 15, retries: int = 3):
    """GET with polite retry on rate limits (429) and transient 5xx errors."""
    backoff = 2
    for attempt in range(retries + 1):
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            if attempt < retries:
                wait = backoff
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                print(f"  API returned {r.status_code}; retrying in {wait}s...")
                time.sleep(wait)
                backoff *= 2
                continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def get_hero_name(hero_id: int) -> str:
    global HERO_NAMES
    if not HERO_NAMES:
        try:
            r = http_get("https://api.opendota.com/api/heroes", timeout=10)
            for h in r.json():
                HERO_NAMES[h["id"]] = h["localized_name"]
        except Exception:
            pass
    return HERO_NAMES.get(hero_id, f"Hero#{hero_id}")


# OpenDota per-player benchmark keys -> human labels. Each carries a `pct`
# (0-1 percentile vs everyone else who played that hero this patch).
BENCHMARK_LABELS = {
    "gold_per_min": "GPM",
    "xp_per_min": "XPM",
    "last_hits_per_min": "Last hits/min",
    "hero_damage_per_min": "Hero damage/min",
    "hero_healing_per_min": "Hero healing/min",
    "tower_damage": "Tower damage",
    "stuns_per_min": "Stun duration/min",
}


def extract_benchmarks(player: dict) -> dict:
    """Pull percentile rankings (0-100) from a parsed player's benchmarks."""
    raw = player.get("benchmarks") or {}
    out = {}
    for key, label in BENCHMARK_LABELS.items():
        entry = raw.get(key)
        if isinstance(entry, dict) and entry.get("pct") is not None:
            out[label] = round(entry["pct"] * 100)
    return out


def fetch_match(match_id: int) -> dict:
    url = f"{OPENDOTA_BASE}/matches/{match_id}"
    r = http_get(url)
    return r.json()


def fetch_recent_matches(account_id: int, limit: int = 20) -> list:
    url = f"{OPENDOTA_BASE}/players/{account_id}/recentMatches"
    r = http_get(url)
    matches = r.json() or []
    return matches[:limit] if limit else matches


def request_parse(match_id: int) -> str | None:
    url = f"{OPENDOTA_BASE}/request/{match_id}"
    r = requests.post(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return str(data.get("job", {}).get("jobId", ""))


def poll_parse(job_id: str, timeout_secs: int = 300) -> bool:
    url = f"{OPENDOTA_BASE}/request/{job_id}"
    deadline = time.time() + timeout_secs
    interval = 5
    while time.time() < deadline:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data is None or data == {}:
                return True
            if isinstance(data, dict) and data.get("type") == "error":
                print(f"  Parse job failed: {data}")
                return False
        time.sleep(interval)
        interval = min(interval * 1.5, 30)
    print("  Parse timed out.")
    return False


def extract_my_metrics(match_data: dict) -> dict:
    player = None
    for p in match_data.get("players", []):
        if p.get("account_id") == ACCOUNT_ID:
            player = p
            break
    if player is None:
        raise ValueError(f"Account {ACCOUNT_ID} not found in match {match_data.get('match_id')}.")

    duration_secs = match_data.get("duration", 0)
    duration_min = duration_secs / 60

    radiant_win = match_data.get("radiant_win", False)
    is_radiant = player.get("isRadiant", player.get("player_slot", 0) < 128)
    won = (radiant_win and is_radiant) or (not radiant_win and not is_radiant)

    kills = player.get("kills", 0)
    deaths = player.get("deaths", 0)
    assists = player.get("assists", 0)
    kda = (kills + assists) / max(deaths, 1)

    hero_id = player.get("hero_id", 0)
    hero_name = get_hero_name(hero_id)

    position = infer_position(match_data, player, is_radiant)
    lane = player.get("lane", 0)
    is_roaming = player.get("is_roaming", False)
    if is_roaming:
        lane_role = f"Roaming (P{position})"
    else:
        lane_name = LANE_NAMES.get(lane, f"Lane {lane}")
        lane_role = f"{lane_name} — P{position}"

    last_hits = player.get("last_hits", 0)
    denies = player.get("denies", 0)
    lh_per_min = last_hits / max(duration_min, 1)

    # CS@10 from the per-minute cumulative last-hit series (parsed matches only).
    lh_t = player.get("lh_t") or []
    cs_at_10 = lh_t[10] if len(lh_t) > 10 else None

    gpm = player.get("gold_per_min", 0)
    xpm = player.get("xp_per_min", 0)
    hero_damage = player.get("hero_damage", 0)
    tower_damage = player.get("tower_damage", 0)
    hero_healing = player.get("hero_healing", 0)

    total_gold = player.get("total_gold", 0)
    net_worth = player.get("net_worth", total_gold)

    obs_placed = player.get("obs_placed", 0)
    sen_placed = player.get("sen_placed", 0)
    camps_stacked = player.get("camps_stacked", 0)
    courier_kills = player.get("courier_kills", 0)

    teamfight_participation = player.get("teamfight_participation", None)
    if teamfight_participation is None:
        teamfight_participation = (kills + assists) / max(
            sum(p.get("kills", 0) for p in match_data.get("players", [])
                if (p.get("isRadiant", p.get("player_slot", 0) < 128)) == is_radiant) or 1,
            1
        )

    pings = player.get("pings", 0)
    actions_per_min = player.get("actions_per_min", 0)

    item_slots = {
        f"item_{i}": player.get(f"item_{i}", 0) for i in range(6)
    }

    purchase_log = player.get("purchase_log", [])
    first_items = [e.get("key") for e in purchase_log[:6]] if purchase_log else []

    benchmarks = extract_benchmarks(player)

    return {
        "match_id": match_data.get("match_id"),
        "result": "WIN" if won else "LOSS",
        "hero": hero_name,
        "role_lane": lane_role,
        "position": position,
        "cs_at_10": cs_at_10,
        "lobby_type": match_data.get("lobby_type"),
        "game_mode": match_data.get("game_mode"),
        "duration_min": round(duration_min, 1),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": round(kda, 2),
        "gpm": gpm,
        "xpm": xpm,
        "last_hits": last_hits,
        "denies": denies,
        "lh_per_min": round(lh_per_min, 1),
        "hero_damage": hero_damage,
        "tower_damage": tower_damage,
        "hero_healing": hero_healing,
        "net_worth": net_worth,
        "obs_placed": obs_placed,
        "sen_placed": sen_placed,
        "camps_stacked": camps_stacked,
        "courier_kills": courier_kills,
        "teamfight_participation": round(float(teamfight_participation), 2) if teamfight_participation is not None else None,
        "pings": pings,
        "actions_per_min": actions_per_min,
        "first_items": first_items,
        "benchmarks": benchmarks,
    }


def build_prompt(metrics: dict, trend_context: str | None = None) -> str:
    m = metrics
    cs10 = m.get("cs_at_10")
    cs10_line = f"- CS @ 10 min: {cs10}\n" if cs10 is not None else ""

    if trend_context:
        history_block = (
            "\n## Your History & Trends\n"
            "The following is computed from your previously analyzed games. "
            "Use it to ground the Trend Note and, where relevant, the improvement points:\n\n"
            f"{trend_context}\n"
        )
        trend_instruction = (
            "Write 2–4 sentences using the history data above. Call out any recurring "
            "issue and whether this game improved on it or repeated it. Reference concrete "
            "numbers (your average vs this game). Do not invent history not shown above."
        )
    else:
        history_block = ""
        trend_instruction = "*(Skipped — insufficient match history for trend analysis.)*"

    benchmarks = m.get("benchmarks") or {}
    if benchmarks:
        bench_lines = "\n".join(
            f"- {label}: {pct}th percentile" for label, pct in benchmarks.items()
        )
        benchmark_block = (
            "\n## Performance Percentiles (vs everyone else on this hero this patch)\n"
            "These are real percentile rankings from OpenDota. Use THEM — not your own "
            "intuition — to decide whether farm, damage, and impact were high or low. "
            "A percentile near 50 is average and is NOT a weakness; only treat something "
            "below ~30th percentile as a genuine shortfall, or above ~70th as a strength.\n"
            f"{bench_lines}\n"
        )
    else:
        benchmark_block = ""

    return f"""You are an experienced Dota 2 coach reviewing a match replay. Below are the stats from my game. Write a coaching report in clean markdown.

CRITICAL RULES — follow these exactly or the report is useless:
1. Base every claim on the stats provided. Do not invent or assume ability mechanics, cooldowns, or damage types. If you mention an ability by name, describe only what the numbers imply, not how the ability works mechanically.
2. Do not state that an ability is channeled, instant-cast, AoE, single-target, or deals a specific damage type unless you are 100% certain — hero kits change between patches and mistakes destroy credibility.
3. Do not assume that a given ability contributes to "hero damage" unless you know it deals direct hero damage. Disables, transforms, and debuffs do not appear in hero damage stats.
4. Tie every coaching point to a specific number from the data.
5. Do NOT judge a stat as "too high" or "too low" in absolute terms. For benchmarked stats use the percentiles below; for everything else use the player's own historical baseline. An average or above-average stat is never a weakness.
6. Deaths and KDA have NO universal benchmark and depend heavily on role and game length. Do NOT apply absolute death thresholds. For a support in a 35+ minute game, 6–9 deaths is normal and not a problem by itself — especially in a win. Only flag deaths if they are clearly above this player's own baseline (see history) or extreme for the game's length. Supports trade their lives to save cores; that is their job.
7. This player is high-MMR ranked. Calibrate to that — do not give beginner-level advice or treat normal high-level trades as mistakes.

## Match Summary
- Match ID: {m['match_id']}
- Result: {m['result']}
- Hero: {m['hero']}
- Role/Lane: {m['role_lane']}
- Duration: {m['duration_min']} minutes
- KDA: {m['kills']}/{m['deaths']}/{m['assists']} ({m['kda']:.2f})
- GPM / XPM: {m['gpm']} / {m['xpm']}
- Last Hits / Denies: {m['last_hits']} / {m['denies']} ({m['lh_per_min']} LH/min)
{cs10_line}- Net Worth: {m['net_worth']:,} gold
- Hero Damage: {m['hero_damage']:,}
- Tower Damage: {m['tower_damage']:,}
- Hero Healing: {m['hero_healing']:,}
- Observer Wards Placed: {m['obs_placed']}
- Sentry Wards Placed: {m['sen_placed']}
- Camps Stacked: {m['camps_stacked']}
- Teamfight Participation: {m['teamfight_participation']}
- Actions per Minute: {m['actions_per_min']}
- Pings: {m['pings']}
- Early items purchased: {', '.join(m['first_items']) if m['first_items'] else 'unknown'}
{benchmark_block}{history_block}
---

Write a coaching report with these exact sections:

# Dota 2 Coaching Report — {m['hero']} ({m['result']})

## Match Overview
One short paragraph summarizing the game: result, hero, role, key numbers.

## Top 3 Things to Improve
For each point: name the issue, tie it to a specific stat from the data above, and give one concrete action to fix it next game. Be direct and specific — not generic advice. Only call something a weakness if a percentile is genuinely low (below ~30th) or it is clearly below this player's own baseline — do not manufacture problems from average stats. If there are fewer than three genuine issues, give fewer; do not pad.

## What You Did Well
1–2 bullet points on genuine strengths from the data — prefer stats at or above the ~70th percentile, or improvements over the player's baseline (skip if the game was genuinely poor; don't invent positives).

## Focus for Next Game
One single, actionable sentence. The single most impactful thing to work on.

## Trend Note
{trend_instruction}

Keep the report under 600 words. Be honest, data-driven, and specific to this hero and role. Do not be sycophantic."""


def generate_report(metrics: dict, trend_context: str | None = None) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(metrics, trend_context=trend_context)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def analyze_match(match_id: int, conn=None, print_report: bool = True) -> bool:
    """Fetch, parse, analyze a match; save the report and persist metrics.

    Returns the metrics dict if a report was produced, or None if the match
    was skipped (e.g. our account wasn't in it). Pass `conn` to persist.
    """
    print(f"Fetching match {match_id}...")
    match_data = fetch_match(match_id)

    if match_data.get("version") is None:
        print("Match is not parsed. Requesting parse...")
        job_id = request_parse(match_id)
        if not job_id:
            print("Failed to start parse job. Proceeding with raw data (some stats may be missing).")
        else:
            print(f"Parse job started (id: {job_id}). Polling for completion...")
            ok = poll_parse(job_id)
            if ok:
                print("Parse complete. Re-fetching match...")
                match_data = fetch_match(match_id)
            else:
                print("Parse did not complete; proceeding with available data.")
    else:
        print("Match is already parsed.")

    print("Extracting metrics...")
    try:
        metrics = extract_my_metrics(match_data)
    except ValueError as e:
        print(f"Error: {e}")
        return None

    # Build the rolling profile from prior games (excluding this one), detect
    # this game's findings, and assemble trend context for the prompt.
    trend_context = None
    current_findings = []
    if conn is not None:
        history = db.get_matches(conn, exclude_match_id=match_id)
        prof = player_profile.compute_profile(history, position=metrics.get("position"))
        current_findings = player_profile.detect_findings(metrics, prof)
        recent_findings = db.get_recent_findings(conn, match_limit=player_profile.RECENT_WINDOW)
        trend_context = player_profile.build_trend_context(
            metrics, prof, current_findings, recent_findings
        )
        if trend_context:
            print(f"  Using history from {prof['n_total']} prior game(s) for trend analysis.")

    print("Generating coaching report with Claude (claude-sonnet-4-6)...")
    report = generate_report(metrics, trend_context=trend_context)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"{match_id}.md"
    report_path.write_text(report, encoding="utf-8")

    if conn is not None:
        db.save_findings(conn, match_id, current_findings)
        db.save_match(conn, metrics)

    if print_report:
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
    print(f"Report saved to {report_path}")
    return metrics


def cmd_analyze(args):
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    try:
        analyze_match(args.match_id, conn=conn)
    finally:
        conn.close()


def is_ranked_match(m: dict) -> bool:
    """True only for ranked matchmaking games (lobby_type 7), excluding turbo."""
    return (m.get("lobby_type") == db.RANKED_LOBBY_TYPE
            and m.get("game_mode") != db.TURBO_GAME_MODE)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def desktop_notify(title: str, message: str) -> None:
    """Best-effort desktop notification; degrades to a terminal banner."""
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="Dota 2 Coach", timeout=10)
        return
    except Exception:
        pass
    # Fallback: terminal bell + prominent banner (works everywhere).
    print(f"\a\n*** {title}: {message} ***\n")


def find_new_matches(conn, limit: int, include_all: bool):
    """Return (recent, eligible, new_matches) — new are unseen, oldest-first."""
    recent = fetch_recent_matches(ACCOUNT_ID, limit=limit)
    if include_all:
        eligible = recent
    else:
        eligible = [m for m in recent if is_ranked_match(m)]
    processed = db.get_processed_ids(conn)
    new_matches = [m for m in eligible if m.get("match_id") not in processed]
    # recentMatches is newest-first; process oldest-first so the rolling
    # profile and trend findings accumulate in chronological order.
    new_matches.reverse()
    return recent, eligible, new_matches


def process_matches(conn, new_matches, notify: bool = False,
                    print_report: bool = True) -> int:
    """Analyze each new match; print the report and optionally notify."""
    count = 0
    for i, m in enumerate(new_matches, 1):
        match_id = m.get("match_id")
        print(f"\n[{i}/{len(new_matches)}] Processing match {match_id}...")
        try:
            metrics = analyze_match(match_id, conn=conn, print_report=print_report)
            if metrics:
                count += 1
                if notify:
                    desktop_notify(
                        "New Dota 2 Coaching Report",
                        f"{metrics['result']} on {metrics['hero']} "
                        f"({metrics['role_lane']}) — saved to reports/{match_id}.md",
                    )
        except requests.HTTPError as e:
            print(f"  Skipping {match_id}: HTTP error: {e}")
        # Be polite to the free API between matches.
        if i < len(new_matches):
            time.sleep(2)
    return count


def cmd_check(args):
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    try:
        print(f"Fetching recent matches for account {ACCOUNT_ID}...")
        recent, eligible, new_matches = find_new_matches(conn, args.limit, args.include_all)

        skipped = len(recent) - len(eligible)
        if skipped:
            print(f"Ignoring {skipped} non-ranked/turbo match(es).")
        print(f"{len(recent)} recent matches found; {len(eligible)} ranked; "
              f"{len(new_matches)} new.")

        if not new_matches:
            print("Nothing new to process. You're up to date.")
            return

        count = process_matches(conn, new_matches, notify=False)
        print(f"\nDone. Processed {count} new match(es). "
              f"Total in DB: {db.count_matches(conn)}.")
    finally:
        conn.close()


def cmd_watch(args):
    conn = db.get_connection(DB_PATH)
    db.init_db(conn)
    interval_secs = max(1, args.interval) * 60
    notify = not args.no_notify
    scope = "matches" if args.include_all else "ranked matches"
    print(f"Watching for new {scope} every {args.interval} min "
          f"(desktop notifications {'on' if notify else 'off'}). Press Ctrl+C to stop.")
    try:
        while True:
            try:
                recent, eligible, new_matches = find_new_matches(conn, args.limit, args.include_all)
                if new_matches:
                    print(f"\n[{_now()}] {len(new_matches)} new match(es) found.")
                    count = process_matches(conn, new_matches, notify=notify)
                    print(f"[{_now()}] Processed {count}. Total in DB: {db.count_matches(conn)}.")
                else:
                    print(f"[{_now()}] No new matches. Next check in {args.interval} min.")
            except requests.RequestException as e:
                print(f"[{_now()}] Network error: {e}. Will retry next cycle.")
            time.sleep(interval_secs)
    except KeyboardInterrupt:
        print("\nStopped watching. Bye.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="coach",
        description="Dota 2 automated post-match coaching reports",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a single match and generate a coaching report")
    analyze_parser.add_argument("match_id", type=int, help="OpenDota match ID")
    analyze_parser.set_defaults(func=cmd_analyze)

    check_parser = subparsers.add_parser("check", help="Process any recent ranked matches not yet in the database")
    check_parser.add_argument("--limit", type=int, default=20,
                              help="How many recent matches to look back over (default: 20)")
    check_parser.add_argument("--include-all", action="store_true",
                              help="Process all game modes, not just ranked (includes turbo/unranked)")
    check_parser.set_defaults(func=cmd_check)

    watch_parser = subparsers.add_parser("watch", help="Poll for new matches on an interval and report automatically")
    watch_parser.add_argument("--interval", type=int, default=5,
                              help="Minutes between checks (default: 5)")
    watch_parser.add_argument("--limit", type=int, default=20,
                              help="How many recent matches to look back over each check (default: 20)")
    watch_parser.add_argument("--include-all", action="store_true",
                              help="Process all game modes, not just ranked (includes turbo/unranked)")
    watch_parser.add_argument("--no-notify", action="store_true",
                              help="Disable desktop notifications when a new report lands")
    watch_parser.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
