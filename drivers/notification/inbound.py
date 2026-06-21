"""macOS Notification Center inbound driver.

Watches usernoted's per-user SQLite store and emits a batched
``notification:new`` event when new Notification Center records land.

There is no stable public API for reading every app's delivered
notifications, so this driver treats usernoted as an OS-owned SQLite
surface: discover the known DB path, cursor at the current max record id
on first run, then watch the WAL with kqueue and query only rows newer
than the cursor.

Requires Full Disk Access for whichever process runs the kernel:
System Settings -> Privacy & Security -> Full Disk Access.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import re
import select
import sqlite3
import string
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from boot import paths
from boot import processes as P

SOURCE = "notification"
STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / SOURCE
CURSOR_PATH = STATE_DIR / "cursor.yaml"

MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Modern macOS stores delivered notifications here. Keep the alternatives
# narrow and explicit: the group containers are privacy-protected, and some
# Terminal contexts can stat a known child path even when directory listing is
# denied.
DB_CANDIDATES = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db",
    Path.home() / "Library" / "Group Containers" / "group.com.apple.UserNotifications" / "db2" / "db",
    Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "Library" / "UserNotifications" / "db2" / "db",
    Path.home() / "Library" / "Group Containers" / "group.com.apple.UserNotifications" / "Library" / "UserNotifications" / "db2" / "db",
)

# NOTE_WRITE covers in-place WAL appends. DELETE/RENAME cover checkpoints or
# file rotation; the watcher re-opens the new file after those events.
VNODE_FLAGS = (
    select.KQ_NOTE_WRITE
    | select.KQ_NOTE_EXTEND
    | select.KQ_NOTE_DELETE
    | select.KQ_NOTE_RENAME
)

HUMAN_STRING_LIMIT = 8


class NotificationStoreError(Exception):
    """Base class for usernoted store failures."""


class NotificationAccessError(NotificationStoreError):
    """Raised when macOS privacy controls block the notification store."""


class UnsupportedSchemaError(NotificationStoreError):
    """Raised when the notification DB does not expose a usable record table."""


@dataclass(frozen=True)
class StoreShape:
    table: str
    id_expr: str
    uuid_expr: str
    delivered_expr: str
    data_expr: str
    title_expr: str
    subtitle_expr: str
    body_expr: str
    bundle_expr: str
    app_name_expr: str
    join_sql: str


def _load_cursor() -> Optional[int]:
    if not CURSOR_PATH.exists():
        return None
    try:
        with CURSOR_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    val = data.get("last_cursor_id")
    return int(val) if val is not None else None


def _save_cursor(last_cursor_id: int) -> None:
    for attempt in range(2):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".yaml.tmp")
        try:
            with tmp.open("w") as f:
                yaml.safe_dump({"last_cursor_id": int(last_cursor_id)}, f)
            os.replace(tmp, CURSOR_PATH)
            return
        except FileNotFoundError:
            if attempt == 1:
                raise


def _is_permission_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, PermissionError)
        or "authorization denied" in text
        or "operation not permitted" in text
        or "not authorized" in text
        or "permission denied" in text
    )


def _find_db(candidates: tuple[Path, ...] = DB_CANDIDATES) -> Path:
    permission_errors: list[str] = []
    for path in candidates:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        except PermissionError as e:
            permission_errors.append(f"{path}: {e}")
            continue
        if not os.path.isfile(path):
            continue
        if st.st_size < 1:
            continue
        return path
    if permission_errors:
        raise NotificationAccessError("; ".join(permission_errors))
    checked = ", ".join(str(p) for p in candidates)
    raise NotificationStoreError(f"notification database not found; checked: {checked}")


def _connect(db_path: Path) -> sqlite3.Connection:
    # Open normally instead of mode=ro so SQLite can read the live WAL state.
    # query_only=ON makes the connection reject writes.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _pick(cols: set[str], *names: str) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    for name in names:
        found = lower.get(name.lower())
        if found is not None:
            return found
    return None


def _expr(alias: str, col: Optional[str]) -> str:
    return f"{alias}.{col}" if col else "NULL"


def _store_shape(conn: sqlite3.Connection) -> StoreShape:
    tables = _table_names(conn)
    if "record" in tables:
        record_cols = _table_columns(conn, "record")
        app_cols = _table_columns(conn, "app") if "app" in tables else set()

        id_col = _pick(record_cols, "rec_id", "record_id", "id")
        id_expr = f"r.{id_col}" if id_col else "r.ROWID"
        uuid_col = _pick(record_cols, "uuid", "identifier", "request_id")
        delivered_col = _pick(
            record_cols,
            "delivered_date",
            "date_presented",
            "presentation_date",
            "request_date",
            "date",
        )
        data_col = _pick(record_cols, "data", "blob", "payload", "archive")
        title_col = _pick(record_cols, "title")
        subtitle_col = _pick(record_cols, "subtitle")
        body_col = _pick(record_cols, "body", "message", "informative_text")

        join_sql = ""
        bundle_expr = "NULL"
        app_name_expr = "NULL"
        app_id_col = _pick(record_cols, "app_id", "application_id")
        app_pk_col = _pick(app_cols, "app_id", "application_id", "id")
        if app_id_col and app_pk_col:
            join_sql = f"LEFT JOIN app a ON a.{app_pk_col} = r.{app_id_col}"
            bundle_col = _pick(
                app_cols,
                "identifier",
                "bundle_identifier",
                "bundle_id",
                "bundleid",
                "app_identifier",
            )
            name_col = _pick(app_cols, "display_name", "name", "title")
            bundle_expr = _expr("a", bundle_col)
            app_name_expr = _expr("a", name_col)
        else:
            bundle_col = _pick(
                record_cols,
                "bundle_identifier",
                "bundle_id",
                "bundleid",
                "app_identifier",
            )
            app_name_col = _pick(record_cols, "app_name", "display_name")
            bundle_expr = _expr("r", bundle_col)
            app_name_expr = _expr("r", app_name_col)

        return StoreShape(
            table="record",
            id_expr=id_expr,
            uuid_expr=_expr("r", uuid_col),
            delivered_expr=_expr("r", delivered_col),
            data_expr=_expr("r", data_col),
            title_expr=_expr("r", title_col),
            subtitle_expr=_expr("r", subtitle_col),
            body_expr=_expr("r", body_col),
            bundle_expr=bundle_expr,
            app_name_expr=app_name_expr,
            join_sql=join_sql,
        )

    for table in ("notifications", "notification"):
        if table not in tables:
            continue
        cols = _table_columns(conn, table)
        id_col = _pick(cols, "id", "rowid", "record_id")
        id_expr = f"r.{id_col}" if id_col else "r.ROWID"
        uuid_col = _pick(cols, "uuid", "identifier", "request_id")
        delivered_col = _pick(cols, "delivered_at", "delivered_date", "date")
        data_col = _pick(cols, "data", "payload", "archive")
        title_col = _pick(cols, "title")
        subtitle_col = _pick(cols, "subtitle")
        body_col = _pick(cols, "body", "message", "informative_text")
        bundle_col = _pick(cols, "bundle_identifier", "bundle_id", "bundleid", "app_identifier")
        app_name_col = _pick(cols, "app_name", "display_name", "name")
        return StoreShape(
            table=table,
            id_expr=id_expr,
            uuid_expr=_expr("r", uuid_col),
            delivered_expr=_expr("r", delivered_col),
            data_expr=_expr("r", data_col),
            title_expr=_expr("r", title_col),
            subtitle_expr=_expr("r", subtitle_col),
            body_expr=_expr("r", body_col),
            bundle_expr=_expr("r", bundle_col),
            app_name_expr=_expr("r", app_name_col),
            join_sql="",
        )

    raise UnsupportedSchemaError(
        f"unsupported notification DB schema; tables={sorted(tables)}"
    )


def _max_cursor(conn: sqlite3.Connection) -> int:
    shape = _store_shape(conn)
    row = conn.execute(
        f"SELECT COALESCE(MAX({shape.id_expr}), 0) AS max_id "
        f"FROM {shape.table} r {shape.join_sql}"
    ).fetchone()
    return int(row["max_id"] or 0)


def _query_rows(conn: sqlite3.Connection, last_cursor_id: int) -> list[sqlite3.Row]:
    shape = _store_shape(conn)
    sql = f"""
    SELECT
        {shape.id_expr} AS cursor_id,
        {shape.uuid_expr} AS notification_id,
        {shape.delivered_expr} AS delivered_at,
        {shape.data_expr} AS data,
        {shape.title_expr} AS title,
        {shape.subtitle_expr} AS subtitle,
        {shape.body_expr} AS body,
        {shape.bundle_expr} AS bundle_id,
        {shape.app_name_expr} AS app_name
    FROM {shape.table} r
    {shape.join_sql}
    WHERE {shape.id_expr} > ?
    ORDER BY {shape.id_expr} ASC
    """
    return conn.execute(sql, (int(last_cursor_id),)).fetchall()


def _bootstrap_cursor(db_path: Path) -> int:
    with _connect(db_path) as conn:
        last = _max_cursor(conn)
    _save_cursor(last)
    return last


def _date_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    # usernoted has historically used Apple absolute time. If the value is
    # larger than 1e9 it is probably Unix seconds; 2001-relative seconds in
    # the 2020s are still below that threshold.
    if ts > 1_000_000_000:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        dt = MAC_EPOCH + timedelta(seconds=ts)
    return dt.astimezone().isoformat(timespec="seconds")


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = str(value).replace("\x00", "").strip()
    if not text:
        return None
    return text


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_FIELD_KEYS = {
    "title": "title",
    "subtitle": "subtitle",
    "body": "body",
    "message": "body",
    "informativetext": "body",
    "threadidentifier": "thread_id",
    "threadid": "thread_id",
    "categoryidentifier": "category_id",
    "categoryid": "category_id",
}

_STRING_BLOCKLIST = {
    "bplist",
    "ns.objects",
    "ns.keys",
    "ns.string",
    "ns.uuidbytes",
    "ns.time",
    "ns.data",
    "nsdictionary",
    "nsmutabledictionary",
    "nsarray",
    "nsmutablearray",
    "nsstring",
    "nsdate",
    "nsuuid",
    "unnotification",
    "unnotificationrequest",
    "unmutablenotificationcontent",
    "unnotificationcontent",
    "unnotificationaction",
    "unnotificationsound",
}


def _resolve_archive(obj: Any) -> Any:
    if not isinstance(obj, dict) or "$objects" not in obj:
        return obj

    objects = obj.get("$objects")
    if not isinstance(objects, list):
        return obj

    def resolve(value: Any, depth: int = 0) -> Any:
        if depth > 20:
            return None
        if isinstance(value, plistlib.UID):
            idx = value.data
            if 0 <= idx < len(objects):
                return resolve(objects[idx], depth + 1)
            return None
        if isinstance(value, dict):
            return {
                str(resolve(k, depth + 1)): resolve(v, depth + 1)
                for k, v in value.items()
                if str(k) != "$class"
            }
        if isinstance(value, list):
            return [resolve(v, depth + 1) for v in value]
        return value

    top = obj.get("$top")
    if isinstance(top, dict) and "root" in top:
        return resolve(top["root"])
    return resolve(objects[1]) if len(objects) > 1 else obj


def _collect_keyed_fields(obj: Any) -> dict[str, str]:
    out: dict[str, str] = {}

    def visit(value: Any, key_hint: Optional[str] = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, str(key))
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child, key_hint)
            return

        if key_hint is None:
            return
        field = _FIELD_KEYS.get(_norm_key(key_hint))
        if field is None:
            return
        text = _clean_text(value)
        if text and field not in out:
            out[field] = text

    visit(obj)
    return out


def _looks_human_string(text: str) -> bool:
    lowered = _norm_key(text)
    if lowered in _STRING_BLOCKLIST:
        return False
    if lowered.startswith("unnotification") or lowered.startswith("ns"):
        return False
    if len(text) < 2:
        return False
    printable = set(string.printable)
    return all((ch in printable or ch.isspace()) for ch in text)


def _extract_printable_strings(data: bytes) -> list[str]:
    strings: list[str] = []
    seen: set[str] = set()
    # UTF-8 / ASCII runs.
    for match in re.finditer(rb"[\x20-\x7e][\x20-\x7e\t\r\n]{2,}", data):
        text = match.group(0).decode("utf-8", errors="ignore").strip()
        if text and text not in seen and _looks_human_string(text):
            strings.append(text)
            seen.add(text)
    # UTF-16LE runs, common in Apple archives.
    for match in re.finditer((rb"(?:[\x20-\x7e]\x00){3,}"), data):
        text = match.group(0).decode("utf-16le", errors="ignore").strip()
        if text and text not in seen and _looks_human_string(text):
            strings.append(text)
            seen.add(text)
    return strings[:HUMAN_STRING_LIMIT]


def _decode_blob_fields(data: Any) -> dict[str, str]:
    if data is None:
        return {}
    if isinstance(data, memoryview):
        data = data.tobytes()
    if not isinstance(data, bytes):
        return {}

    fields: dict[str, str] = {}
    try:
        plist = plistlib.loads(data)
    except Exception:
        plist = None
    if plist is not None:
        resolved = _resolve_archive(plist)
        fields.update(_collect_keyed_fields(resolved))

    if not any(fields.get(k) for k in ("title", "body", "subtitle")):
        strings = _extract_printable_strings(data)
        if strings:
            fields["text"] = " | ".join(strings[:4])
    return fields


def _row_payload(row: sqlite3.Row) -> dict:
    blob_fields = _decode_blob_fields(row["data"])
    cursor_id = int(row["cursor_id"])

    payload = {
        "id": str(row["notification_id"] or cursor_id),
        "cursor_id": cursor_id,
        "delivered_at": _date_to_iso(row["delivered_at"]),
        "bundle_id": _clean_text(row["bundle_id"]),
        "app_name": _clean_text(row["app_name"]),
        "title": _clean_text(row["title"]) or blob_fields.get("title"),
        "subtitle": _clean_text(row["subtitle"]) or blob_fields.get("subtitle"),
        "body": _clean_text(row["body"]) or blob_fields.get("body"),
        "text": blob_fields.get("text"),
        "thread_id": blob_fields.get("thread_id"),
        "category_id": blob_fields.get("category_id"),
    }
    return {k: v for k, v in payload.items() if v is not None}


def _drain_since(db_path: Path, last_cursor_id: int) -> tuple[int, list[dict]]:
    with _connect(db_path) as conn:
        rows = _query_rows(conn, last_cursor_id)
    new_last = int(last_cursor_id)
    notifications: list[dict] = []
    for row in rows:
        cursor_id = int(row["cursor_id"])
        new_last = max(new_last, cursor_id)
        notifications.append(_row_payload(row))
    return new_last, notifications


def _emit_notifications(db_path: Path, notifications: list[dict]) -> None:
    P.emit_event({
        "source": SOURCE,
        "kind": "new",
        "db_path": str(db_path),
        "notifications": notifications,
    })
    print(f"[notification-in] emitted {len(notifications)} notification(s)", flush=True)


def _emit_watch_failed(reason: str, detail: str, db_path: Optional[Path] = None) -> None:
    P.emit_event({
        "source": SOURCE,
        "kind": "watch_failed",
        "reason": reason,
        "detail": detail,
        "db_path": str(db_path) if db_path is not None else None,
    })
    print(f"[notification-in] watch failed: {reason}: {detail}", flush=True)


def _drain_live(db_path: Path, last_cursor_id: int) -> int:
    new_last, notifications = _drain_since(db_path, last_cursor_id)
    if notifications:
        _emit_notifications(db_path, notifications)
        _save_cursor(new_last)
    return new_last


class _KqueueWatcher:
    """Watch the notification DB family via kqueue from a background thread."""

    def __init__(self, db_path: Path, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.db_path = db_path
        self.loop = loop
        self.queue = queue
        self._kq: Optional[select.kqueue] = None
        self._fds: dict[int, Path] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_fd_r: Optional[int] = None
        self._stop_fd_w: Optional[int] = None

    def _target_paths(self) -> tuple[Path, ...]:
        return (
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
            self.db_path,
        )

    def _open_target(self, path: Path) -> Optional[int]:
        try:
            return os.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            return None

    def _register(self, fd: int, path: Path) -> None:
        assert self._kq is not None
        kev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=VNODE_FLAGS,
        )
        self._kq.control([kev], 0)
        self._fds[fd] = path

    def start(self) -> None:
        self._kq = select.kqueue()
        r, w = os.pipe()
        self._stop_fd_r, self._stop_fd_w = r, w
        stop_kev = select.kevent(
            r,
            filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        )
        self._kq.control([stop_kev], 0)

        for path in self._target_paths():
            try:
                fd = self._open_target(path)
            except PermissionError as e:
                raise NotificationAccessError(str(e)) from e
            if fd is not None:
                self._register(fd, path)
                print(f"[notification-in] kqueue watching {path.name} fd={fd}", flush=True)

        if not self._fds:
            raise NotificationStoreError(f"no watchable notification DB files near {self.db_path}")

        self._thread = threading.Thread(
            target=self._loop,
            name="notification-in-kq",
            daemon=True,
        )
        self._thread.start()

    def _reopen(self, old_fd: int, path: Path) -> None:
        try:
            os.close(old_fd)
        except OSError:
            pass
        self._fds.pop(old_fd, None)
        try:
            fd = self._open_target(path)
        except PermissionError as e:
            print(f"[notification-in] kq reopen permission error: {e}", flush=True)
            return
        if fd is not None:
            self._register(fd, path)
            print(f"[notification-in] kqueue re-opened {path.name} fd={fd}", flush=True)

    def _loop(self) -> None:
        assert self._kq is not None
        assert self._stop_fd_r is not None
        try:
            while True:
                events = self._kq.control([], 16, None)
                stop = False
                for ev in events:
                    if ev.ident == self._stop_fd_r:
                        stop = True
                        continue
                    fd = ev.ident
                    path = self._fds.get(fd)
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                    if path is not None and ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        print(f"[notification-in] kq {path.name} rotated, reopening", flush=True)
                        self._reopen(fd, path)
                if stop:
                    return
        except Exception as e:
            print(f"[notification-in] kq-loop crashed: {e!r}", flush=True)

    def stop(self) -> None:
        if self._stop_fd_w is not None:
            try:
                os.write(self._stop_fd_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        for fd in (self._stop_fd_r, self._stop_fd_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._stop_fd_r = self._stop_fd_w = None
        if self._kq is not None:
            self._kq.close()
            self._kq = None


async def run() -> None:
    try:
        db_path = _find_db()
    except NotificationAccessError as e:
        _emit_watch_failed("permission_denied", str(e))
        return
    except NotificationStoreError as e:
        _emit_watch_failed("not_found", str(e))
        return

    last_cursor_id = _load_cursor()
    if last_cursor_id is None:
        try:
            last_cursor_id = await asyncio.to_thread(_bootstrap_cursor, db_path)
        except sqlite3.Error as e:
            reason = "permission_denied" if _is_permission_error(e) else "bootstrap_failed"
            _emit_watch_failed(reason, str(e), db_path)
            return
        except NotificationStoreError as e:
            _emit_watch_failed("unsupported_schema", str(e), db_path)
            return
        print(f"[notification-in] bootstrap cursor last_cursor_id={last_cursor_id}", flush=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    watcher = _KqueueWatcher(db_path, loop, queue)
    try:
        watcher.start()
    except NotificationAccessError as e:
        _emit_watch_failed("permission_denied", str(e), db_path)
        return
    except NotificationStoreError as e:
        _emit_watch_failed("watch_unavailable", str(e), db_path)
        return

    print(f"[notification-in] started, last_cursor_id={last_cursor_id}", flush=True)

    # Catch notifications that arrived between kernel downtime and watcher
    # startup. First run already bootstrapped to "now", so this does not replay
    # historical records.
    try:
        last_cursor_id = await asyncio.to_thread(_drain_live, db_path, last_cursor_id)
    except sqlite3.Error as e:
        print(f"[notification-in] catch-up drain error: {e!r}", flush=True)

    try:
        while True:
            await queue.get()
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                last_cursor_id = await asyncio.to_thread(
                    _drain_live,
                    db_path,
                    last_cursor_id,
                )
            except sqlite3.Error as e:
                reason = "permission_denied" if _is_permission_error(e) else "query_failed"
                _emit_watch_failed(reason, str(e), db_path)
                return
    except asyncio.CancelledError:
        raise
    finally:
        watcher.stop()
        print("[notification-in] stopped", flush=True)
