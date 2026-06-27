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
    metrics_json            TEXT
);
"""

# Metric keys persisted as their own columns (order is not significant).
_COLUMN_KEYS = [
    "result", "hero", "role_lane", "duration_min", "kills", "deaths",
    "assists", "kda", "gpm", "xpm", "last_hits", "denies", "lh_per_min",
    "hero_damage", "tower_damage", "hero_healing", "net_worth", "obs_placed",
    "sen_placed", "camps_stacked", "courier_kills", "teamfight_participation",
    "pings", "actions_per_min",
]


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


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
