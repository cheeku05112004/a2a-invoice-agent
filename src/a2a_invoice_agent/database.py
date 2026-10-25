import sqlite3
import hashlib
import json
import secrets
import os

from pathlib import Path
from contextlib import contextmanager
import os


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def translate(sql):
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        sql = sql.replace(
            "created_at <= datetime('now', '-2 minutes')",
            "created_at <= CURRENT_TIMESTAMP - INTERVAL '2 minutes'",
        )
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        sql = sql.replace("?", "%s")
        sql = sql.replace(
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        )
        if "INSERT INTO decision_cache" in sql:
            sql += " ON CONFLICT (package_hash) DO NOTHING"
        return sql

    def execute(self, sql, params=()):
        return self.connection.execute(self.translate(sql), params)

    def executemany(self, sql, params):
        cursor = self.connection.cursor()
        cursor.executemany(self.translate(sql), params)
        return cursor


DB_PATH = Path(os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent.parent.parent / "invoice_agent.db")))


class IdempotencyConflict(Exception):
    pass


class TaskStateConflict(Exception):
    pass


class MessageInProgress(Exception):
    pass


@contextmanager
def get_connection():
    database_url = os.getenv("DATABASE_URL", "").strip()

    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        raw_connection = psycopg.connect(
            database_url,
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=15,
        )
        try:
            yield PostgresConnection(raw_connection)
        finally:
            raw_connection.close()
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
    finally:
        connection.close()


def canonical_message_hash(message):
    canonical = json.dumps(
        message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_db():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                context_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                state TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal, message_id)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                reservation_token TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (principal, message_id)
            )
        """)

        if os.getenv("DATABASE_URL", "").strip():
            columns = {
                row["column_name"]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                    AND table_name = 'reservations'
                    """
                ).fetchall()
            }
        else:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(reservations)"
                ).fetchall()
            }
        if "reservation_token" not in columns:
            connection.execute(
                "ALTER TABLE reservations ADD COLUMN reservation_token TEXT"
            )

        connection.execute("""
            CREATE TABLE IF NOT EXISTS decision_cache (
                package_hash TEXT PRIMARY KEY,
                proposal_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_principal
            ON tasks(principal)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_task
            ON messages(task_id)
        """)


def reserve_message(principal, message_id, message_hash):
    token = secrets.token_urlsafe(32)

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        try:
            existing = connection.execute(
                """
                SELECT message_hash, task_id
                FROM messages
                WHERE principal = ? AND message_id = ?
                """,
                (principal, message_id),
            ).fetchone()

            if existing:
                if existing["message_hash"] != message_hash:
                    raise IdempotencyConflict()

                connection.execute("COMMIT")
                return {
                    "status": "completed",
                    "task_id": existing["task_id"],
                }

            connection.execute(
                """
                DELETE FROM reservations
                WHERE principal = ?
                AND message_id = ?
                AND created_at <= datetime('now', '-2 minutes')
                """,
                (principal, message_id),
            )

            existing = connection.execute(
                """
                SELECT message_hash
                FROM reservations
                WHERE principal = ? AND message_id = ?
                """,
                (principal, message_id),
            ).fetchone()

            if existing:
                if existing["message_hash"] != message_hash:
                    raise IdempotencyConflict()

                connection.execute("COMMIT")
                return {"status": "processing"}

            connection.execute(
                """
                INSERT INTO reservations (
                    principal, message_id, message_hash,
                    reservation_token
                )
                VALUES (?, ?, ?, ?)
                """,
                (principal, message_id, message_hash, token),
            )

            connection.execute("COMMIT")
            return {
                "status": "reserved",
                "reservation_token": token,
            }

        except Exception:
            connection.execute("ROLLBACK")
            raise


def release_reservation(
    principal, message_id, message_hash, reservation_token
):
    if not reservation_token:
        return

    with get_connection() as connection:
        connection.execute(
            """
            DELETE FROM reservations
            WHERE principal = ?
            AND message_id = ?
            AND message_hash = ?
            AND reservation_token = ?
            """,
            (principal, message_id, message_hash, reservation_token),
        )


def save_task_idempotently(
    principal,
    message_id,
    message_hash,
    reservation_token,
    task_id,
    context_id,
    batch_id,
    state,
    task_json,
):
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        try:
            existing = connection.execute(
                """
                SELECT message_hash, task_id
                FROM messages
                WHERE principal = ? AND message_id = ?
                """,
                (principal, message_id),
            ).fetchone()

            if existing:
                if existing["message_hash"] != message_hash:
                    raise IdempotencyConflict()

                connection.execute("COMMIT")
                return {
                    "created": False,
                    "task_id": existing["task_id"],
                }

            reservation = connection.execute(
                """
                SELECT message_hash, reservation_token
                FROM reservations
                WHERE principal = ? AND message_id = ?
                """,
                (principal, message_id),
            ).fetchone()

            if (
                reservation is None
                or reservation["message_hash"] != message_hash
                or reservation["reservation_token"] != reservation_token
                or not reservation_token
            ):
                raise MessageInProgress()

            connection.execute(
                """
                INSERT INTO tasks (
                    id, principal, context_id,
                    batch_id, state, task_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    principal,
                    context_id,
                    batch_id,
                    state,
                    task_json,
                ),
            )

            connection.execute(
                """
                INSERT INTO messages (
                    principal, message_id,
                    message_hash, task_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (principal, message_id, message_hash, task_id),
            )

            connection.execute(
                """
                DELETE FROM reservations
                WHERE principal = ?
                AND message_id = ?
                AND reservation_token = ?
                """,
                (principal, message_id, reservation_token),
            )

            connection.execute("COMMIT")

            return {
                "created": True,
                "task_id": task_id,
            }

        except Exception:
            connection.execute("ROLLBACK")
            raise


def get_task_by_id(task_id, principal):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM tasks
            WHERE id = ? AND principal = ?
            """,
            (task_id, principal),
        ).fetchone()

        return dict(row) if row else None


def list_tasks_by_principal(principal):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT task_json
            FROM tasks
            WHERE principal = ?
            ORDER BY created_at DESC
            """,
            (principal,),
        ).fetchall()

        return [json.loads(row["task_json"]) for row in rows]


def update_task_atomically(
    task_id,
    principal,
    expected_states,
    new_state,
    task_json,
):
    if not expected_states:
        raise ValueError("expected_states cannot be empty")

    placeholders = ",".join("?" for _ in expected_states)

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        try:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                SET state = ?, task_json = ?
                WHERE id = ?
                AND principal = ?
                AND state IN ({placeholders})
                """,
                (
                    new_state,
                    task_json,
                    task_id,
                    principal,
                    *expected_states,
                ),
            )

            if cursor.rowcount != 1:
                raise TaskStateConflict()

            connection.execute("COMMIT")

        except Exception:
            connection.execute("ROLLBACK")
            raise


def complete_task_with_message_idempotently(
    task_id,
    principal,
    message_id,
    message_hash,
    task_json,
):
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        try:
            existing = connection.execute(
                """
                SELECT message_hash, task_id
                FROM messages
                WHERE principal = ? AND message_id = ?
                """,
                (principal, message_id),
            ).fetchone()

            if existing:
                if existing["message_hash"] != message_hash:
                    raise IdempotencyConflict()

                row = connection.execute(
                    """
                    SELECT task_json FROM tasks
                    WHERE id = ? AND principal = ?
                    """,
                    (existing["task_id"], principal),
                ).fetchone()

                if row is None:
                    raise TaskStateConflict()

                connection.execute("COMMIT")
                return {
                    "created": False,
                    "task": json.loads(row["task_json"]),
                }

            cursor = connection.execute(
                """
                UPDATE tasks
                SET state = ?, task_json = ?
                WHERE id = ?
                AND principal = ?
                AND state = ?
                """,
                (
                    "TASK_STATE_COMPLETED",
                    task_json,
                    task_id,
                    principal,
                    "TASK_STATE_INPUT_REQUIRED",
                ),
            )

            if cursor.rowcount != 1:
                raise TaskStateConflict()

            connection.execute(
                """
                INSERT INTO messages (
                    principal, message_id,
                    message_hash, task_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (principal, message_id, message_hash, task_id),
            )

            connection.execute("COMMIT")
            return {
                "created": True,
                "task": json.loads(task_json),
            }

        except Exception:
            connection.execute("ROLLBACK")
            raise


def get_cached_decisions(package_hashes):
    if not package_hashes:
        return {}

    placeholders = ",".join("?" for _ in package_hashes)
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT package_hash, proposal_json FROM decision_cache "
            f"WHERE package_hash IN ({placeholders})",
            tuple(package_hashes),
        ).fetchall()

    return {
        row["package_hash"]: json.loads(row["proposal_json"])
        for row in rows
    }


def save_cached_decisions(decisions):
    if not decisions:
        return

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """
                INSERT OR IGNORE INTO decision_cache
                    (package_hash, proposal_json)
                VALUES (?, ?)
                """,
                [
                    (
                        package_hash,
                        json.dumps(
                            proposal,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                    )
                    for package_hash, proposal in decisions.items()
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def get_message_task(principal, message_id, message_hash):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT message_hash, task_id
            FROM messages
            WHERE principal = ? AND message_id = ?
            """,
            (principal, message_id),
        ).fetchone()

        if row is None:
            return None

        if row["message_hash"] != message_hash:
            raise IdempotencyConflict()

        task = connection.execute(
            """
            SELECT task_json FROM tasks
            WHERE id = ? AND principal = ?
            """,
            (row["task_id"], principal),
        ).fetchone()

        return json.loads(task["task_json"]) if task else None
