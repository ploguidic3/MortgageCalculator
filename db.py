"""SQLite persistence for processed matches.

Milestone 2: stores each processed match's key metrics + a timestamp so the
`check` command can skip matches it has already analyzed. The full metrics
dict is also stored as JSON for later use (trend analysis in Milestone 3).
"""
import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "coach.db"

# Columns mirrored from the metrics dict for easy querying. The full dict is
# also kept in metrics_json so we never lose data as the schema evolves.
SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id                INTEGER PRIMARY KEY,
    processed_at            TEXT    NOT NULL,
    result                  TEXT,
    hero                    TEXT,
    role_lane               TEXT,
    position                INTEGER,
    duration_min            REAL,
    kills                   INTEGER,
    deaths                  INTEGER,
    assists                 INTEGER,
    kda                     REAL,
    gpm                     INTEGER,
    xpm                     INTEGER,
    last_hits               INTEGER,
    denies                  INTEGER,
    lh_per_min              REAL,
    cs_at_10                INTEGER,
    hero_damage             INTEGER,
    tower_damage            INTEGER,
    hero_healing            INTEGER,
    net_worth               INTEGER,
    obs_placed              INTEGER,
    sen_placed              INTEGER,
    camps_stacked           INTEGER,
    courier_kills           INTEGER,
    teamfight_participation REAL,
    pings                   INTEGER,
    actions_per_min         INTEGER,
    lobby_type              INTEGER,
    game_mode               INTEGER,
    metrics_json            TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id     INTEGER NOT NULL,
    finding_key  TEXT    NOT NULL,
    label        TEXT,
    metric       TEXT,
    metric_value REAL,
    baseline     REAL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_match ON findings(match_id);
"""

# Metric keys persisted as their own columns (order is not significant).
_COLUMN_KEYS = [
    "result", "hero", "role_lane", "position", "duration_min", "kills",
    "deaths", "assists", "kda", "gpm", "xpm", "last_hits", "denies",
    "lh_per_min", "cs_at_10", "hero_damage", "tower_damage", "hero_healing",
    "net_worth", "obs_placed", "sen_placed", "camps_stacked", "courier_kills",
    "teamfight_participation", "pings", "actions_per_min", "lobby_type",
    "game_mode",
]

# Columns added after the original Milestone 2 schema; applied to pre-existing
# databases via ALTER TABLE so old coach.db files keep working.
_MIGRATION_COLUMNS = {
    "position": "INTEGER",
    "cs_at_10": "INTEGER",
    "lobby_type": "INTEGER",
    "game_mode": "INTEGER",
}

RANKED_LOBBY_TYPE = 7
TURBO_GAME_MODE = 23


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(matches)")}
    for col, decl in _MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {decl}")


def match_exists(conn: sqlite3.Connection, match_id: int) -> bool:
    cur = conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,))
    return cur.fetchone() is not None


def get_processed_ids(conn: sqlite3.Connection) -> set:
    cur = conn.execute("SELECT match_id FROM matches")
    return {row["match_id"] for row in cur.fetchall()}


def save_match(conn: sqlite3.Connection, metrics: dict) -> None:
    """Insert or replace a match row from a metrics dict."""
    columns = ["match_id", "processed_at"] + _COLUMN_KEYS + ["metrics_json"]
    values = [
        metrics.get("match_id"),
        datetime.now(timezone.utc).isoformat(),
    ]
    values += [metrics.get(k) for k in _COLUMN_KEYS]
    values.append(json.dumps(metrics))

    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(columns)
    conn.execute(
        f"INSERT OR REPLACE INTO matches ({col_list}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def count_matches(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS n FROM matches")
    return cur.fetchone()["n"]


def get_matches(conn: sqlite3.Connection, exclude_match_id: int | None = None,
                ranked_only: bool = True) -> list:
    """Return stored matches as plain dicts, oldest processed first.

    With ranked_only (the default), turbo and known non-ranked games are
    excluded from trend history. Rows predating lobby/mode tracking have NULL
    values and are kept, so existing history isn't lost.
    """
    if ranked_only:
        cur = conn.execute(
            """SELECT * FROM matches
               WHERE (lobby_type = ? OR lobby_type IS NULL)
                 AND (game_mode != ? OR game_mode IS NULL)
               ORDER BY processed_at ASC""",
            (RANKED_LOBBY_TYPE, TURBO_GAME_MODE),
        )
    else:
        cur = conn.execute("SELECT * FROM matches ORDER BY processed_at ASC")
    rows = [dict(r) for r in cur.fetchall()]
    if exclude_match_id is not None:
        rows = [r for r in rows if r.get("match_id") != exclude_match_id]
    return rows


def save_findings(conn: sqlite3.Connection, match_id: int, findings: list) -> None:
    """Replace the logged findings for a match (idempotent re-processing)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM findings WHERE match_id = ?", (match_id,))
    conn.executemany(
        """INSERT INTO findings
           (match_id, finding_key, label, metric, metric_value, baseline, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (match_id, f.get("key"), f.get("label"), f.get("metric"),
             f.get("value"), f.get("baseline"), now)
            for f in findings
        ],
    )
    conn.commit()


def get_recent_findings(conn: sqlite3.Connection, match_limit: int = 5) -> list:
    """Findings logged for the most recently processed matches.

    Returns a list of dicts including each finding's match_id, so callers can
    count how many distinct recent games each issue appeared in.
    """
    recent_ids = [
        row["match_id"]
        for row in conn.execute(
            "SELECT match_id FROM matches ORDER BY processed_at DESC LIMIT ?",
            (match_limit,),
        )
    ]
    if not recent_ids:
        return []
    placeholders = ", ".join("?" for _ in recent_ids)
    cur = conn.execute(
        f"SELECT * FROM findings WHERE match_id IN ({placeholders})",
        recent_ids,
    )
    return [dict(r) for r in cur.fetchall()]
