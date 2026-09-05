"""
Persistência local (SQLite, stdlib) do face_agent: quem está cadastrado em cada
terminal — com foto e agenda — e até onde cada terminal já foi varrido em
busca de eventos de reconhecimento (poll cursor). Existe pro agente decidir
enroll/revoke por horário e não reprocessar eventos já vistos SEM depender da
nuvem estar no ar — mesmo racional do RosterStore/PollCursorStore do z-edu
(agente-local/src/queue/{rosterStore,pollCursorStore,openDb}.ts), portado pra
SQLite puro em vez de better-sqlite3.
"""
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "face_agent.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roster (
    terminal_id TEXT NOT NULL,
    employee_no TEXT NOT NULL,
    name TEXT NOT NULL,
    jpeg BLOB NOT NULL,
    access_schedule TEXT,
    enrolled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (terminal_id, employee_no)
);
CREATE TABLE IF NOT EXISTS poll_cursor (
    terminal_id TEXT PRIMARY KEY,
    last_event_time TEXT,
    search_id TEXT
);
"""


@dataclass
class RosterEntry:
    terminal_id: str
    employee_no: str
    name: str
    jpeg: bytes
    access_schedule: Optional[dict]
    enrolled: bool


@dataclass
class PollCursor:
    terminal_id: str
    last_event_time: Optional[str] = None
    search_id: Optional[str] = None


class LocalStore:
    """Conexão única de SQLite (WAL) reaproveitada pra roster e poll cursor —
    mesmo arquivo, poucos writes por segundo, sempre no mesmo processo. Lock
    próprio porque o roster é escrito tanto pelos handlers de socket quanto
    pelo schedule_enforcer, em threads diferentes."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- roster ---

    def upsert_roster(self, terminal_id: str, employee_no: str, name: str, jpeg: bytes,
                       access_schedule: Optional[dict]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO roster (terminal_id, employee_no, name, jpeg, access_schedule, enrolled)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(terminal_id, employee_no) DO UPDATE SET
                    name = excluded.name, jpeg = excluded.jpeg, access_schedule = excluded.access_schedule
                """,
                (terminal_id, employee_no, name, jpeg, json.dumps(access_schedule) if access_schedule else None),
            )
            self._conn.commit()

    def set_enrolled(self, terminal_id: str, employee_no: str, enrolled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE roster SET enrolled = ? WHERE terminal_id = ? AND employee_no = ?",
                (1 if enrolled else 0, terminal_id, employee_no),
            )
            self._conn.commit()

    def list_by_terminal(self, terminal_id: str) -> list[RosterEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT terminal_id, employee_no, name, jpeg, access_schedule, enrolled FROM roster WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchall()
        return [
            RosterEntry(
                terminal_id=r[0], employee_no=r[1], name=r[2], jpeg=r[3],
                access_schedule=json.loads(r[4]) if r[4] else None, enrolled=bool(r[5]),
            )
            for r in rows
        ]

    def remove_roster(self, terminal_id: str, employee_no: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM roster WHERE terminal_id = ? AND employee_no = ?", (terminal_id, employee_no)
            )
            self._conn.commit()

    # --- poll cursor ---

    def get_cursor(self, terminal_id: str) -> Optional[PollCursor]:
        with self._lock:
            row = self._conn.execute(
                "SELECT terminal_id, last_event_time, search_id FROM poll_cursor WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()
        if row is None:
            return None
        return PollCursor(terminal_id=row[0], last_event_time=row[1], search_id=row[2])

    def set_cursor(self, cursor: PollCursor) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO poll_cursor (terminal_id, last_event_time, search_id) VALUES (?, ?, ?)
                ON CONFLICT(terminal_id) DO UPDATE SET
                    last_event_time = excluded.last_event_time, search_id = excluded.search_id
                """,
                (cursor.terminal_id, cursor.last_event_time, cursor.search_id),
            )
            self._conn.commit()
