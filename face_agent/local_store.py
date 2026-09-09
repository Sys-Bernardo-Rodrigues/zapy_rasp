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
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "face_agent.sqlite3")

# Retenção de eventos (e das fotos que carregam junto, mesma linha): corte por tempo
# corrido, não por contagem — evento com `time` mais velho que isso é apagado sozinho a
# cada add_events (nunca some antes da hora).
EVENTS_RETENTION_DAYS = 365

# `time` do evento sempre vem com -03:00 (Hikvision manda o relógio do próprio device;
# Intelbras fixa -03:00 no code, ver intelbras_client.py) — mesmo offset aqui pra
# comparação de string funcionar direito (ISO 8601 só ordena lexicograficamente quando
# todo mundo usa o mesmo offset).
_BR_TZ = timezone(timedelta(hours=-3))

# ponytail: sem teto de linhas, só corte por tempo (foi o pedido: "365 dias, sobrescrevendo
# automaticamente"). Terminal com tráfego muito alto pode acumular bastante dentro do ano;
# se o disco virar problema, some um cap de contagem por cima disso.

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
CREATE TABLE IF NOT EXISTS events (
    dedupe_key TEXT PRIMARY KEY,
    terminal_id TEXT NOT NULL,
    employee_no TEXT,
    time TEXT NOT NULL,
    direction TEXT,
    success INTEGER NOT NULL,
    source TEXT NOT NULL,
    picture BLOB,
    device_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (time DESC);
CREATE TABLE IF NOT EXISTS terminal_names (
    terminal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """Migração idempotente pra bancos criados antes de uma coluna existir — `CREATE TABLE
    IF NOT EXISTS` não adiciona colunas novas numa tabela já existente."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


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


@dataclass
class AccessEvent:
    dedupe_key: str
    terminal_id: str
    employee_no: Optional[str]
    time: str
    direction: str
    success: bool
    source: str
    has_picture: bool
    name: Optional[str] = None  # nome do roster (join), None se a pessoa não está cadastrada


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
        _ensure_column(self._conn, "events", "picture", "BLOB")
        _ensure_column(self._conn, "events", "device_name", "TEXT")
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

    def get_roster_name(self, terminal_id: str, employee_no: str) -> Optional[str]:
        """Nome cadastrado — usado pra casar um evento com quem passou. `None` também
        cobre "sem foto de cadastro" (jpeg é NOT NULL no roster, então achar a linha
        já garante que a foto existe)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM roster WHERE terminal_id = ? AND employee_no = ?",
                (terminal_id, employee_no),
            ).fetchone()
        return row[0] if row else None

    def get_photo(self, terminal_id: str, employee_no: str) -> Optional[bytes]:
        with self._lock:
            row = self._conn.execute(
                "SELECT jpeg FROM roster WHERE terminal_id = ? AND employee_no = ?",
                (terminal_id, employee_no),
            ).fetchone()
        return row[0] if row else None

    # --- events (log local de auditoria — alimenta o painel "Eventos") ---

    def add_events(self, events: list[dict]) -> None:
        if not events:
            return
        with self._lock:
            # Upsert em vez de INSERT OR IGNORE: reprocessar o mesmo dedupe_key (ex.: cursor
            # rebobinado) não duplica, e ainda preenche picture/device_name se tinham ficado
            # nulos da primeira vez (foto que falhou, ou terminal que não mandava nome ainda)
            # — sem sobrescrever o que já tem valor.
            self._conn.executemany(
                """
                INSERT INTO events (dedupe_key, terminal_id, employee_no, time, direction, success, source, picture, device_name)
                VALUES (:dedupe_key, :terminal_id, :employee_no, :time, :direction, :success, :source, :picture, :device_name)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    picture = COALESCE(events.picture, excluded.picture),
                    device_name = COALESCE(events.device_name, excluded.device_name)
                """,
                [{**e, "success": 1 if e["success"] else 0, "picture": e.get("picture"), "device_name": e.get("device_name")} for e in events],
            )
            cutoff = (datetime.now(_BR_TZ) - timedelta(days=EVENTS_RETENTION_DAYS)).isoformat()
            self._conn.execute("DELETE FROM events WHERE time < ?", (cutoff,))
            self._conn.commit()

    # Nome do roster (Zapy/ZAccess) tem prioridade — é a identidade "oficial" sincronizada;
    # cai pro nome que o próprio terminal já manda no evento (device_name, ex.: cardholder
    # name do Hikvision) quando a pessoa nunca foi cadastrada por aqui (enrolada direto no
    # device, fora do fluxo do ZAccess) — sem isso o evento só mostra a matrícula crua.
    _EVENT_SELECT = (
        "SELECT e.dedupe_key, e.terminal_id, e.employee_no, e.time, e.direction, e.success, "
        "e.source, e.picture IS NOT NULL, COALESCE(r.name, e.device_name) FROM events e "
        "LEFT JOIN roster r ON r.terminal_id = e.terminal_id AND r.employee_no = e.employee_no"
    )

    @staticmethod
    def _row_to_event(r) -> AccessEvent:
        return AccessEvent(
            dedupe_key=r[0], terminal_id=r[1], employee_no=r[2], time=r[3],
            direction=r[4], success=bool(r[5]), source=r[6], has_picture=bool(r[7]), name=r[8],
        )

    def query_events(
        self, *, terminal_id: str | None = None, success: Optional[bool] = None,
        start: str | None = None, end: str | None = None, q: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[AccessEvent], int]:
        """Eventos filtrados/paginados pro painel de Eventos — devolve (página, total sem
        limit/offset) pra montar o paginador. `q` casa contra employee_no ou nome (roster,
        via join)."""
        where: list[str] = []
        params: dict = {}
        if terminal_id:
            where.append("e.terminal_id = :terminal_id")
            params["terminal_id"] = terminal_id
        if success is not None:
            where.append("e.success = :success")
            params["success"] = 1 if success else 0
        if start:
            where.append("e.time >= :start")
            params["start"] = start
        if end:
            where.append("e.time <= :end")
            params["end"] = end
        if q:
            where.append(
                "(e.employee_no LIKE :q ESCAPE '\\' OR r.name LIKE :q ESCAPE '\\' "
                "OR e.device_name LIKE :q ESCAPE '\\')"
            )
            params["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM events e LEFT JOIN roster r "
                f"ON r.terminal_id = e.terminal_id AND r.employee_no = e.employee_no {where_sql}",
                params,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"{self._EVENT_SELECT} {where_sql} ORDER BY e.time DESC LIMIT :limit OFFSET :offset",
                {**params, "limit": limit, "offset": offset},
            ).fetchall()
        return [self._row_to_event(r) for r in rows], total

    def list_recent_events(self, limit: int = 100) -> list[AccessEvent]:
        events, _ = self.query_events(limit=limit)
        return events

    def get_event(self, dedupe_key: str) -> Optional[AccessEvent]:
        with self._lock:
            row = self._conn.execute(f"{self._EVENT_SELECT} WHERE e.dedupe_key = ?", (dedupe_key,)).fetchone()
        return self._row_to_event(row) if row else None

    def list_event_terminal_ids(self) -> list[str]:
        """IDs de terminal com pelo menos um evento — pra popular o filtro por terminal
        no painel, sem depender de o terminal ainda estar cadastrado em algum lugar."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT terminal_id FROM events").fetchall()
        return [r[0] for r in rows]

    # --- nomes de terminal (ZAccess é quem sabe o nome; id "id do ZAccess" != id local
    # de face_terminals_store.py — o painel de Eventos precisa desse nome pra exibir) ---

    def set_terminal_name(self, terminal_id: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO terminal_names (terminal_id, name) VALUES (?, ?) "
                "ON CONFLICT(terminal_id) DO UPDATE SET name = excluded.name",
                (terminal_id, name),
            )
            self._conn.commit()

    def get_terminal_name(self, terminal_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM terminal_names WHERE terminal_id = ?", (terminal_id,)
            ).fetchone()
        return row[0] if row else None

    def get_event_picture(self, dedupe_key: str) -> Optional[bytes]:
        """Foto capturada NO MOMENTO do evento (pictureURL do AcsEvent, baixada uma vez e
        persistida aqui — o device pode rotacionar/apagar o arquivo original depois)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT picture FROM events WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        return row[0] if row and row[0] else None

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
