from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings


def _db_path() -> Path:
    path = Path(settings.sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                lead_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                selected_slot_start TEXT NOT NULL,
                selected_slot_end TEXT NOT NULL,
                status TEXT NOT NULL,
                calendar_event_id TEXT,
                vapi_call_id TEXT,
                call_plan_json TEXT,
                transcript TEXT,
                qualification_json TEXT
            );

            CREATE TABLE IF NOT EXISTS transcription_jobs (
                job_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                recording_url TEXT,
                status TEXT NOT NULL,
                raw_response_json TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (lead_id)
            );

            CREATE TABLE IF NOT EXISTS vapi_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id TEXT,
                call_id TEXT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "leads", "vapi_call_id", "TEXT")


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_lead(payload: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                lead_id, created_at, name, email, phone, address, message,
                selected_slot_start, selected_slot_end, status, calendar_event_id,
                call_plan_json
            )
            VALUES (
                :lead_id, :created_at, :name, :email, :phone, :address, :message,
                :selected_slot_start, :selected_slot_end, :status, :calendar_event_id,
                :call_plan_json
            )
            """,
            payload,
        )


def get_lead(lead_id: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()


def get_lead_by_vapi_call_id(call_id: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM leads WHERE vapi_call_id = ?",
            (call_id,),
        ).fetchone()


def update_call_plan(lead_id: str, call_plan: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE leads SET call_plan_json = ? WHERE lead_id = ?",
            (json.dumps(call_plan, ensure_ascii=False), lead_id),
        )


def update_vapi_call(lead_id: str, call_id: str, status: str = "call_scheduled") -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE leads SET vapi_call_id = ?, status = ? WHERE lead_id = ?",
            (call_id, status, lead_id),
        )


def update_status(lead_id: str, status: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE leads SET status = ? WHERE lead_id = ?",
            (status, lead_id),
        )


def add_vapi_event(
    *,
    lead_id: str | None,
    call_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO vapi_events (
                lead_id, call_id, created_at, event_type, payload_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                call_id,
                now_iso(),
                event_type,
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def add_transcription_job(
    job_id: str,
    lead_id: str,
    recording_url: str | None,
    raw_response: dict[str, Any],
) -> None:
    with connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO transcription_jobs (
                job_id, lead_id, created_at, recording_url, status, raw_response_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                lead_id,
                now_iso(),
                recording_url,
                "submitted",
                json.dumps(raw_response, ensure_ascii=False),
            ),
        )


def complete_transcription(
    lead_id: str,
    transcript: str,
    qualification: dict[str, Any],
) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = ?, transcript = ?, qualification_json = ?
            WHERE lead_id = ?
            """,
            (
                "transcribed",
                transcript,
                json.dumps(qualification, ensure_ascii=False),
                lead_id,
            ),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("call_plan_json"):
        data["call_plan"] = json.loads(data["call_plan_json"])
    if data.get("qualification_json"):
        data["qualification"] = json.loads(data["qualification_json"])
    return data
