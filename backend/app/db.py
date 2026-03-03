from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('pdf', 'csv')),
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS resource_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    version_label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'failed')),
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    resource_version_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('pdf', 'csv')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'failed', 'succeeded')),
    error_summary TEXT,
    anomaly_report_json TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE,
    FOREIGN KEY(resource_version_id) REFERENCES resource_versions(id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_version_id INTEGER NOT NULL,
    external_card_key TEXT NOT NULL,
    chapter TEXT,
    question_number INTEGER,
    question_raw TEXT NOT NULL,
    question_norm TEXT NOT NULL,
    answer_key TEXT NOT NULL CHECK (answer_key IN ('A', 'B', 'C', 'D', 'E')),
    answer_text_raw TEXT,
    answer_text_norm TEXT,
    explanation_raw TEXT,
    explanation_norm TEXT,
    state TEXT NOT NULL CHECK (state IN ('draft', 'published', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(resource_version_id) REFERENCES resource_versions(id) ON DELETE CASCADE,
    UNIQUE(resource_version_id, external_card_key)
);

CREATE TABLE IF NOT EXISTS card_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    option_key TEXT NOT NULL CHECK (option_key IN ('A', 'B', 'C', 'D', 'E')),
    text_raw TEXT NOT NULL,
    text_norm TEXT NOT NULL,
    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE,
    UNIQUE(card_id, option_key)
);

CREATE TABLE IF NOT EXISTS card_parse_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,
    warning_code TEXT NOT NULL,
    warning_detail TEXT,
    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_card_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 0,
    times_correct INTEGER NOT NULL DEFAULT 0,
    times_incorrect INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    last_result TEXT CHECK (last_result IN ('correct', 'incorrect')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE,
    UNIQUE(user_id, card_id)
);

CREATE TABLE IF NOT EXISTS study_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    card_id INTEGER,
    event_type TEXT NOT NULL,
    meta_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS user_archived_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    UNIQUE(user_id, card_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_resource_versions_resource ON resource_versions(resource_id);
CREATE INDEX IF NOT EXISTS idx_cards_version ON cards(resource_version_id);
CREATE INDEX IF NOT EXISTS idx_cards_state ON cards(state);
CREATE INDEX IF NOT EXISTS idx_user_progress_user ON user_card_progress(user_id);
CREATE INDEX IF NOT EXISTS idx_study_events_user ON study_events(user_id);
CREATE INDEX IF NOT EXISTS idx_import_jobs_version ON import_jobs(resource_version_id);
"""


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    db_path = Path(current_app.config["DB_PATH"])
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
