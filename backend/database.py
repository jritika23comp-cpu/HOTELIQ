from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "hoteliq.db")))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'RevPAR Strategist',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    booking_url TEXT,
    category TEXT,
    rating REAL,
    total_rooms INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    is_portfolio INTEGER NOT NULL DEFAULT 0,
    room_type TEXT DEFAULT 'Standard Room',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracking_conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    guests INTEGER NOT NULL DEFAULT 2,
    rooms INTEGER NOT NULL DEFAULT 1,
    stay_duration INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    tracking_condition_id INTEGER,
    room_type TEXT,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    source TEXT NOT NULL DEFAULT 'DEMO DATA',
    observed_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS availability_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    room_type TEXT,
    available INTEGER NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'DEMO DATA',
    observed_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS detected_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    percentage_change REAL,
    detected_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    estimated_rooms_sold REAL,
    estimated_occupancy REAL,
    estimated_revenue REAL,
    methodology TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    version TEXT NOT NULL,
    training_date TEXT NOT NULL,
    metrics TEXT NOT NULL,
    dataset_version TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL,
    prediction_type TEXT NOT NULL,
    prediction_value REAL NOT NULL,
    confidence REAL,
    predicted_for TEXT NOT NULL,
    model_version TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(property_id) REFERENCES properties(id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    supporting_data TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    records_written INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS collection_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_prop_time ON price_snapshots(property_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_avail_prop_time ON availability_snapshots(property_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, created_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
