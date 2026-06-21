from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import plistlib

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.notification import inbound  # noqa: E402


def _make_usernoted_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE app (
                app_id INTEGER PRIMARY KEY,
                identifier TEXT,
                display_name TEXT
            );
            CREATE TABLE record (
                rec_id INTEGER PRIMARY KEY,
                app_id INTEGER,
                uuid TEXT,
                delivered_date REAL,
                data BLOB
            );
            """
        )
        conn.execute(
            "INSERT INTO app (app_id, identifier, display_name) VALUES (1, ?, ?)",
            ("com.example.deploy", "Deploys"),
        )
        conn.execute(
            """
            INSERT INTO record
                (rec_id, app_id, uuid, delivered_date, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "old",
                804_000_000,
                plistlib.dumps({"title": "Old", "body": "Already seen"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO record
                (rec_id, app_id, uuid, delivered_date, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                2,
                1,
                "new",
                804_000_060,
                plistlib.dumps(
                    {
                        "title": "Deploy finished",
                        "subtitle": "pai/main",
                        "body": "All checks passed",
                        "threadIdentifier": "ci-thread",
                        "categoryIdentifier": "ci",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_drains_only_rows_newer_than_cursor(tmp_path: Path) -> None:
    db = tmp_path / "db"
    _make_usernoted_db(db)

    new_cursor, notifications = inbound._drain_since(db, 1)

    assert new_cursor == 2
    assert notifications == [
        {
            "id": "new",
            "cursor_id": 2,
            "delivered_at": (
                inbound.MAC_EPOCH + timedelta(seconds=804_000_060)
            ).astimezone().isoformat(timespec="seconds"),
            "bundle_id": "com.example.deploy",
            "app_name": "Deploys",
            "title": "Deploy finished",
            "subtitle": "pai/main",
            "body": "All checks passed",
            "thread_id": "ci-thread",
            "category_id": "ci",
        }
    ]


def test_max_cursor_bootstraps_without_replay(tmp_path: Path) -> None:
    db = tmp_path / "db"
    _make_usernoted_db(db)

    with inbound._connect(db) as conn:
        assert inbound._max_cursor(conn) == 2

    new_cursor, notifications = inbound._drain_since(db, 2)
    assert new_cursor == 2
    assert notifications == []


def test_supports_flat_notification_table(tmp_path: Path) -> None:
    db = tmp_path / "db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY,
                bundle_id TEXT,
                app_name TEXT,
                title TEXT,
                body TEXT,
                delivered_at INTEGER
            );
            INSERT INTO notifications
                (id, bundle_id, app_name, title, body, delivered_at)
            VALUES
                (9, 'com.example.todo', 'Todo', 'Reminder', 'Submit report', 1780000000);
            """
        )
        conn.commit()
    finally:
        conn.close()

    new_cursor, notifications = inbound._drain_since(db, 0)

    assert new_cursor == 9
    assert notifications == [
        {
            "id": "9",
            "cursor_id": 9,
            "delivered_at": datetime.fromtimestamp(
                1780000000,
                tz=timezone.utc,
            ).astimezone().isoformat(timespec="seconds"),
            "bundle_id": "com.example.todo",
            "app_name": "Todo",
            "title": "Reminder",
            "body": "Submit report",
        }
    ]


def test_blob_fallback_extracts_human_text() -> None:
    fields = inbound._decode_blob_fields(b"\x00UNNotificationRequest\x00\x00Hello there\x00Body text")

    assert fields == {"text": "Hello there | Body text"}


def test_unsupported_schema_is_explicit(tmp_path: Path) -> None:
    db = tmp_path / "db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with inbound._connect(db) as conn:
        try:
            inbound._max_cursor(conn)
        except inbound.UnsupportedSchemaError as e:
            assert "unsupported notification DB schema" in str(e)
        else:
            raise AssertionError("expected UnsupportedSchemaError")
