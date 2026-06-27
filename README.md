# Dota 2 Automated Post-Match Coaching Reports

A command-line tool that pulls your finished Dota 2 matches from OpenDota,
analyzes your play from the parsed replay data, and writes a prioritized
coaching report using Claude. It remembers the matches it has already
processed so it never reports on the same game twice.

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- Your OpenDota account ID (the number in your
  `https://www.opendota.com/players/<id>` URL)

## ⚠️ Enable public match data first

OpenDota can only see your matches if you have **Expose Public Match Data**
turned on in Dota 2:

> Dota 2 → Settings → Options → Advanced → **Expose Public Match Data** ✅

Without this, OpenDota will have no record of your games and the tool will
find nothing to analyze. This only affects matches played *after* you enable it.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env from the template
cp .env.example .env
```

Then edit `.env` and fill in your values:

```
DOTA_ACCOUNT_ID=20013127
ANTHROPIC_API_KEY=sk-ant-api03-...
```

> **Windows note:** save `.env` as UTF-8, not UTF-16. If you use Notepad,
> choose "UTF-8" in the encoding dropdown of the Save dialog (the tool also
> tries to recover from a UTF-16 file automatically).

Your `.env` is gitignored and will never be committed.

## Usage

### Analyze a single match

```bash
python coach.py analyze <match_id>
```

Fetches the match, requests a parse if it isn't parsed yet (this can take a
minute — progress is shown), extracts your stats, generates a coaching
report, prints it, and saves it to `reports/<match_id>.md`.

### Check for new matches

```bash
python coach.py check                # ranked games from your 20 most recent
python coach.py check --limit 5      # only the 5 most recent
python coach.py check --include-all  # don't skip turbo/unranked
```

Pulls your recent matches, processes any not already in the local database
(`coach.db`), and skips the ones it has seen before. Running it twice in a
row only processes each match once.

**Only ranked matchmaking games are processed by default** — turbo and
unranked games are ignored (pass `--include-all` to override). They are also
excluded from your trend baselines, so turbo's inflated GPM never skews your
averages.

## How it works

- **OpenDota API** (free, no auth) provides match data. A match must be
  *parsed* (`version` field non-null) for full stats; the tool triggers a
  parse and polls until it's ready.
- Your stats are matched out of the match's `players[]` array by account ID.
- A clean metrics summary — not raw JSON — is sent to Claude
  (`claude-sonnet-4-6`) with guardrails against inventing hero ability
  mechanics.
- Real **percentile benchmarks** from OpenDota (your GPM, XPM, last hits,
  damage, etc. vs everyone else on that hero this patch) are included so
  the coach judges stats against actual data instead of guessing absolute
  thresholds. Deaths/KDA are judged relative to role, game length, and your
  own baseline — a support dying several times in a long game isn't flagged
  as a mistake.
- Processed matches are stored in `coach.db` (SQLite) so `check` is
  incremental.

## Memory & trends

As you process more matches, the tool builds a **rolling profile** of your
play: role-aware baselines (deaths, KDA, GPM/XPM, CS@10, ward counts, etc.)
computed from your recent games. Each report compares the current game to
your own averages, and the **Trend Note** section calls out recurring issues
and whether you improved on them.

Role-sensitive stats (GPM, XPM, CS@10, last hits, wards) are compared **only
against your games in the same position** — your P1 carry's farm is never
averaged against your P5 support's. Role-neutral stats (deaths, KDA,
teamfight %) may fall back to an all-roles baseline when same-role history is
still thin.

Under the hood, each game's notable deviations from your baseline are logged
to a `findings` table, so later reports can say things like *"High deaths:
flagged in 3 recent games; not flagged this game — improvement."* You need a
few games in the database before trends appear (it falls back to a no-history
note until then).

## Files

| File              | Purpose                                            |
| ----------------- | -------------------------------------------------- |
| `coach.py`        | CLI entry point and analysis flow                  |
| `db.py`           | SQLite persistence for matches and findings        |
| `profile.py`      | Rolling baselines, finding detection, trend context|
| `reports/`        | Generated markdown reports (one per match)         |
| `coach.db`        | Local match database (gitignored, created on first run) |
| `.env`            | Your secrets (gitignored)                          |

## Roadmap

- **Milestone 1** ✅ — single-match analysis and reports
- **Milestone 2** ✅ — SQLite persistence + `check` for new matches
- **Milestone 3** ✅ — rolling player profile, baselines, and trend-aware feedback
- **Milestone 4** — `watch` mode that processes new matches automatically
