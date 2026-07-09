"""Slack inbound — Socket Mode bot.

Holds a Socket Mode WebSocket (app-level `xapp-` token) and wakes PAI on two
event kinds only: DMs to the bot (`message.im`) and @-mentions of the bot
(`app_mention`). Plain channel firehose is ignored.

Socket Mode only delivers events that occur *while connected* — anything that
lands during a reboot/deploy window is lost by the socket. So this driver
combines whatsapp's live-socket model with imessage's cursor model:

  - A cursor `sys/drivers/slack/cursor.yaml = {last_ts}` records the newest
    Slack ts seen (atomic tmp+rename, mirroring imessage/inbound.py).
  - On boot, BEFORE opening the socket, `_catchup` polls
    conversations.history(oldest=last_ts) for every conversation the bot is in,
    writes day-files, and emits ONE coalesced `slack:backlog`. A fresh install
    (no cursor) bootstraps the cursor to "now" so it never replays all history.
  - Live: each Socket Mode envelope is acked immediately, then written to a
    day-file, emitted as `slack:new`, and the cursor advanced.

Day-file shape mirrors whatsapp: one dir per Slack conversation under
`var/spool/communication/slack/<slug>/`, one `.md` per day, each line
`[HH:MM] <sender>: <text>`. `meta.yaml` carries `channel: slack`, the Slack
`slack_channel` id, `channel_type`, and the `thread_ts` a reply should thread
into — slack-out reads it to address chat.postMessage.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from boot import paths
from boot import processes as P

from drivers.slack import tokens as slack_tokens

# ── paths ──────────────────────────────────────────────────────────
PAI_ROOT = paths.PAI_ROOT
MESSAGES_ROOT = paths.var_spool_communication() / "slack"
PEOPLE_ROOT = paths.var_lib_memory() / "people"
STATE_DIR = PAI_ROOT / "sys" / "drivers" / "slack"
CURSOR_PATH = STATE_DIR / "cursor.yaml"

# The Socket Mode listener runs in the SDK's own threads, so cursor writes can
# race the boot-thread catch-up. One lock serializes read-compare-write.
_cursor_lock = threading.Lock()

# users_info / conversations_info are stable for the life of a process — cache
# so a busy channel doesn't hammer the Web API resolving the same ids.
_user_cache: dict[str, str] = {}
_channel_name_cache: dict[str, str] = {}


# ── cursor ─────────────────────────────────────────────────────────
def _ts_float(ts: Optional[str]) -> float:
    try:
        return float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _load_cursor() -> Optional[str]:
    if not CURSOR_PATH.exists():
        return None
    try:
        with CURSOR_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    val = data.get("last_ts")
    return str(val) if val is not None else None


def _write_cursor(last_ts: str) -> None:
    # Retry once: tmp/ under sys/ can be reaped between mkdir and rename.
    for attempt in range(2):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".yaml.tmp")
        try:
            with tmp.open("w") as f:
                yaml.safe_dump({"last_ts": str(last_ts)}, f)
            os.replace(tmp, CURSOR_PATH)
            return
        except FileNotFoundError:
            if attempt == 1:
                raise


def _advance_cursor(ts: Optional[str]) -> None:
    """Move the cursor to `ts` iff it is strictly newer. Thread-safe."""
    if not ts:
        return
    with _cursor_lock:
        cur = _load_cursor()
        if cur is not None and _ts_float(cur) >= _ts_float(ts):
            return
        _write_cursor(str(ts))


# ── contact / channel resolution ───────────────────────────────────
def _resolve_user_name(web, user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    if user_id in _user_cache:
        return _user_cache[user_id]
    name = ""
    try:
        u = (web.users_info(user=user_id).get("user") or {})
        prof = u.get("profile") or {}
        name = u.get("real_name") or prof.get("real_name") or prof.get("display_name") or ""
    except Exception as e:  # noqa: BLE001 — a lookup miss just falls back to the id
        print(f"[slack-in] users_info({user_id}) failed: {e}", flush=True)
    _user_cache[user_id] = name
    return name


def _resolve_channel_name(web, channel_id: Optional[str]) -> str:
    if not channel_id:
        return ""
    if channel_id in _channel_name_cache:
        return _channel_name_cache[channel_id]
    name = ""
    try:
        name = (web.conversations_info(channel=channel_id).get("channel") or {}).get("name") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[slack-in] conversations_info({channel_id}) failed: {e}", flush=True)
    _channel_name_cache[channel_id] = name
    return name


def _lookup_person(user_id: str, real_name: str) -> tuple[str, str]:
    """Map a Slack user to (thread slug, display name). Matches an explicit
    `slack:<user_id>` handle first, then the resolved real name against a
    person's `name`, in memory/people/<slug>/about.yaml. Falls back to the raw
    Slack user id as the slug (mirrors whatsapp using a raw phone number)."""
    name = real_name or user_id
    if PEOPLE_ROOT.exists():
        for entry in PEOPLE_ROOT.iterdir():
            if not entry.is_dir():
                continue
            about = entry / "about.yaml"
            if not about.exists():
                continue
            try:
                data = yaml.safe_load(about.read_text()) or {}
            except Exception:  # noqa: BLE001
                continue
            handles = data.get("handles") or []
            if any(isinstance(h, str) and h.strip() == f"slack:{user_id}" for h in handles):
                return (entry.name, data.get("name") or name)
            if real_name and str(data.get("name") or "").strip().lower() == real_name.strip().lower():
                return (entry.name, data.get("name") or name)
    return (user_id, name)


# ── day-file writer ────────────────────────────────────────────────
def _ensure_thread_dir(slug: str, meta_updates: dict) -> Path:
    thread_dir = MESSAGES_ROOT / slug
    thread_dir.mkdir(parents=True, exist_ok=True)
    meta_path = thread_dir / "meta.yaml"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = yaml.safe_load(meta_path.read_text()) or {}
        except yaml.YAMLError:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    changed = False
    if meta.get("channel") != "slack":
        meta["channel"] = "slack"
        changed = True
    for k, v in meta_updates.items():
        if v is not None and meta.get(k) != v:
            meta[k] = v
            changed = True
    if changed or not meta_path.exists():
        meta_path.write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))
    return thread_dir


def _write_message(thread_dir: Path, sender: str, text: str, ts: Optional[str] = None) -> Path:
    when = datetime.now()
    if ts:
        try:
            when = datetime.fromtimestamp(float(ts))
        except (TypeError, ValueError, OSError):
            pass
    day_file = thread_dir / f"{when.strftime('%Y-%m-%d')}.md"
    hm = when.strftime("%H:%M")
    # Every line starts with `[HH:MM] sender:` so a multi-line message stays a
    # parseable log (day-file invariant, shared with whatsapp/imessage).
    prefix = f"[{hm}] {sender}: "
    body = "".join(prefix + ln + "\n" for ln in text.splitlines() or [""])
    with day_file.open("a") as f:
        f.write(body)
    return day_file


def _clean_text(text: str, bot_user_id: str) -> str:
    """Drop the bot's own `<@Uxxxx>` mention token so PAI sees clean text."""
    if not text:
        return ""
    if bot_user_id:
        text = text.replace(f"<@{bot_user_id}>", "").strip()
    return text


def _record_message(
    web,
    *,
    channel: str,
    channel_type: str,
    user_id: Optional[str],
    text: str,
    ts: str,
    thread_ts: Optional[str],
) -> Optional[dict]:
    """Write the day-file + meta for one inbound message. Returns a payload
    dict describing it (for the live event / backlog), or None to skip."""
    if not text:
        return None
    real_name = _resolve_user_name(web, user_id)
    if channel_type == "im":
        slug, sender = _lookup_person(user_id or "", real_name)
        reply_thread_ts: Optional[str] = None  # DMs are unthreaded
    else:
        cname = _resolve_channel_name(web, channel)
        slug = f"#{cname}" if cname else channel
        sender = real_name or (user_id or "")
        # Reply into the mention's thread — its root if it was in one, else its
        # own ts so our reply starts the thread under it.
        reply_thread_ts = thread_ts or ts
    thread_dir = _ensure_thread_dir(slug, {
        "slack_channel": channel,
        "channel_type": channel_type,
        "thread_ts": reply_thread_ts,
    })
    day_file = _write_message(thread_dir, sender, text, ts)
    return {
        "thread": slug,
        "channel": channel,
        "channel_type": channel_type,
        "sender": sender,
        "text": text,
        "thread_ts": reply_thread_ts,
        "day_file": str(day_file.relative_to(PAI_ROOT)),
    }


# ── event emit ─────────────────────────────────────────────────────
def _emit_backlog(records: list[dict]) -> None:
    threads_map: dict[str, dict] = {}
    day_files_by_thread: dict[str, set[str]] = {}
    for m in records:
        slug = m["thread"]
        t = threads_map.setdefault(slug, {"thread": slug, "inbound": 0})
        t["inbound"] += 1
        t["last_text"] = m["text"]
        if df := m.get("day_file"):
            day_files_by_thread.setdefault(slug, set()).add(df)
    threads = list(threads_map.values())
    for t in threads:
        dfs = day_files_by_thread.get(t["thread"])
        if dfs:
            t["day_files"] = sorted(dfs)
    P.emit_event({
        "source": "slack",
        "kind": "backlog",
        "since": datetime.now(timezone.utc).isoformat(),
        "threads": threads,
        "total": len(records),
    })
    print(
        f"[slack-in] emitted backlog ({len(records)} messages across {len(threads_map)} threads)",
        flush=True,
    )


# ── wakeability filter ─────────────────────────────────────────────
def _is_wakeable_history(msg: dict, channel_type: str, bot_user_id: str) -> bool:
    """Whether a conversations.history row should wake PAI: a real user message
    (no subtype/bot), and — for a channel — one that @-mentions the bot."""
    if msg.get("subtype"):
        return False
    if msg.get("bot_id"):
        return False
    user = msg.get("user")
    if not user or user == bot_user_id:
        return False
    if channel_type == "im":
        return True
    return bool(bot_user_id and f"<@{bot_user_id}>" in (msg.get("text") or ""))


# ── boot catch-up ──────────────────────────────────────────────────
def _catchup(web, bot_user_id: str, last_ts: Optional[str]) -> str:
    """Poll every conversation the bot is in for messages newer than the
    cursor, write day-files, emit one coalesced backlog, and return the newest
    ts seen. Runs in a worker thread (blocking Web API)."""
    newest = last_ts or "0"
    convos: list[tuple[dict, str]] = []
    try:
        resp = web.users_conversations(types="im", limit=200)
        convos.extend((c, "im") for c in resp.get("channels", []))
    except Exception as e:  # noqa: BLE001
        print(f"[slack-in] users_conversations(im) failed: {e}", flush=True)
    try:
        resp = web.users_conversations(types="public_channel,private_channel", limit=200)
        convos.extend((c, "channel") for c in resp.get("channels", []))
    except Exception as e:  # noqa: BLE001
        print(f"[slack-in] users_conversations(channels) failed: {e}", flush=True)

    records: list[dict] = []
    for c, ctype in convos:
        cid = c.get("id")
        if not cid:
            continue
        try:
            hist = web.conversations_history(channel=cid, oldest=last_ts or "0", limit=100)
        except Exception as e:  # noqa: BLE001
            print(f"[slack-in] conversations_history({cid}) failed: {e}", flush=True)
            continue
        # history returns newest-first; walk oldest-first so day-files append
        # in chronological order.
        for msg in reversed(hist.get("messages", [])):
            ts = msg.get("ts") or ""
            # `oldest` is inclusive — skip the boundary message we already have.
            if last_ts and _ts_float(ts) <= _ts_float(last_ts):
                continue
            # Advance past everything scanned (even non-mentions) so the cursor
            # doesn't re-scan channel chatter next boot.
            if _ts_float(ts) > _ts_float(newest):
                newest = ts
            if not _is_wakeable_history(msg, ctype, bot_user_id):
                continue
            rec = _record_message(
                web,
                channel=cid,
                channel_type=ctype,
                user_id=msg.get("user"),
                text=_clean_text(msg.get("text", ""), bot_user_id),
                ts=ts,
                thread_ts=msg.get("thread_ts"),
            )
            if rec:
                records.append(rec)

    if records:
        _emit_backlog(records)
    if newest and newest != (last_ts or "0"):
        _advance_cursor(newest)
    print(f"[slack-in] catchup complete ({len(records)} wakeable, cursor={newest})", flush=True)
    return newest


# ── live Socket Mode handling ──────────────────────────────────────
def _handle_live_event(web, bot_user_id: str, event: dict) -> None:
    etype = event.get("type")
    if etype not in ("message", "app_mention"):
        return
    channel = event.get("channel") or ""
    ts = event.get("ts") or ""
    thread_ts = event.get("thread_ts")
    user = event.get("user")
    text = event.get("text") or ""

    if etype == "message":
        # DMs only — the channel firehose is ignored.
        if event.get("channel_type") != "im":
            return
        if event.get("subtype") or event.get("bot_id"):
            return  # edits, joins, bot posts
        if not user or user == bot_user_id:
            return
        channel_type = "im"
    else:  # app_mention
        if event.get("bot_id") or not user or user == bot_user_id:
            return
        # An @-mention inside a DM also fires message.im, which already
        # handles it — don't double-record.
        if channel.startswith("D"):
            return
        channel_type = "channel"

    rec = _record_message(
        web,
        channel=channel,
        channel_type=channel_type,
        user_id=user,
        text=_clean_text(text, bot_user_id),
        ts=ts,
        thread_ts=thread_ts,
    )
    if not rec:
        return
    P.emit_event({"source": "slack", "kind": "new", **rec})
    _advance_cursor(ts)
    print(f"[slack-in] emitted message from {rec['sender']} → {rec['thread']}", flush=True)


def _make_listener(web, bot_user_id: str):
    from slack_sdk.socket_mode.response import SocketModeResponse

    def process(client, req) -> None:  # runs in the SDK's listener thread
        # Ack the envelope FIRST so Slack doesn't redeliver while we process.
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception as e:  # noqa: BLE001
            print(f"[slack-in] ack failed: {e}", flush=True)
        if req.type != "events_api":
            return
        try:
            event = (req.payload or {}).get("event") or {}
            _handle_live_event(web, bot_user_id, event)
        except Exception as e:  # noqa: BLE001
            print(f"[slack-in] event handler error: {e!r}", flush=True)

    return process


async def run() -> None:
    print("[slack-in] starting", flush=True)
    app_tok = slack_tokens.app_token()
    bot_tok = slack_tokens.bot_token()
    if not app_tok or not bot_tok:
        print("[slack-in] tokens missing — run `slack_setup` to paste them; driver idle", flush=True)
        return

    try:
        from slack_sdk.web import WebClient
        from slack_sdk.socket_mode import SocketModeClient
    except Exception as e:  # noqa: BLE001
        print(f"[slack-in] slack_sdk not installed ({e}); driver idle", flush=True)
        return

    web = WebClient(token=bot_tok)
    try:
        auth = web.auth_test()
        bot_user_id = auth.get("user_id") or ""
        print(f"[slack-in] authed as {auth.get('user')} ({bot_user_id})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[slack-in] auth_test failed ({e}); driver idle", flush=True)
        return

    last_ts = _load_cursor()
    if last_ts is None:
        # Fresh install: start from now so we never dump the whole history as
        # a backlog nudge (mirrors imessage bootstrapping to MAX(ROWID)).
        last_ts = f"{datetime.now().timestamp():.6f}"
        _advance_cursor(last_ts)
        print(f"[slack-in] bootstrapped cursor at {last_ts}", flush=True)
    else:
        try:
            last_ts = await asyncio.to_thread(_catchup, web, bot_user_id, last_ts)
        except Exception as e:  # noqa: BLE001
            print(f"[slack-in] catchup failed: {e!r}", flush=True)

    client = SocketModeClient(app_token=app_tok, web_client=web)
    client.socket_mode_request_listeners.append(_make_listener(web, bot_user_id))

    await asyncio.to_thread(client.connect)
    print("[slack-in] socket mode connected", flush=True)

    try:
        await asyncio.Event().wait()  # keep the coroutine alive until cancelled
    except asyncio.CancelledError:
        raise
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        print("[slack-in] stopped", flush=True)
