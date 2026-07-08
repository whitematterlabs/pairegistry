"""Cowork window-activity sidecar.

NSWorkspace didActivateApplication notifications only deliver on a
main-thread runloop (probed 2026-07-07: 0/3 on a background-thread runloop,
3/3 on main) — and the kernel's main thread belongs to asyncio. So this tiny
subprocess owns a main thread: it registers the observer, pumps the runloop,
enriches each activation (AX window title/document, browser tab URL via
AppleScript, idle seconds via Quartz), samples the pasteboard changeCount,
and prints one JSON object per line to stdout. The in-kernel
`drivers.cowork.window_activity` process supervises it and turns lines into
ndjson log entries + kernel events.

Line types:
  {"type": "window", "app", "window", "pid", "ts", "idle_seconds", "url"?, "file_path"?}
  {"type": "clipboard", "app", "content", "clip_type", "ts"}
  {"type": "fatal", "reason"}   then exit

Runs standalone (no `boot` imports) so it only needs pyobjc.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

# Sampled on each activation; seeded on the first one so a copy made before
# this process started is never retroactively logged.
_last_change_count: int | None = None

_BROWSER_URL_SCRIPTS = {
    "Safari": 'tell application "Safari" to return URL of current tab of front window',
    "Google Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of front window',
}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _idle_seconds() -> float:
    import Quartz

    state = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)
    return round(
        Quartz.CGEventSourceSecondsSinceLastEventType(
            state, int(Quartz.kCGAnyInputEventType)
        ),
        1,
    )


def _focused_window(pid: int) -> tuple[str | None, str | None]:
    """(window_title, document_path) via AX. Best-effort — (None, None) on
    any AX error (no permission, app has no windows, wedged app, ...)."""
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        kAXDocumentAttribute,
        kAXFocusedWindowAttribute,
        kAXTitleAttribute,
    )

    try:
        app = AXUIElementCreateApplication(pid)
        err, win = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
        if err != 0 or win is None:
            return None, None
        _, title = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
        _, doc = AXUIElementCopyAttributeValue(win, kAXDocumentAttribute, None)
        path = None
        if doc:
            s = str(doc)
            if s.startswith("file://"):
                from urllib.parse import unquote, urlparse

                path = unquote(urlparse(s).path)
            elif s.startswith("/"):
                path = s
        return (str(title) if title else None), path
    except Exception:
        return None, None


def _browser_url(app_name: str) -> str | None:
    script = _BROWSER_URL_SCRIPTS.get(app_name)
    if not script:
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url.startswith(("http", "file")):
            return url
    except Exception:
        pass
    return None


def _sample_clipboard(app_name: str) -> None:
    global _last_change_count
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pb = NSPasteboard.generalPasteboard()
    count = int(pb.changeCount())
    if _last_change_count is None or count == _last_change_count:
        _last_change_count = count
        return
    _last_change_count = count
    content = pb.stringForType_(NSPasteboardTypeString)
    if content is not None:
        text: str | None = str(content)
        clip_type = "string"
    else:
        text = None
        types = {str(t) for t in (pb.types() or [])}
        if "public.file-url" in types:
            clip_type = "file-url"
        elif types & {"public.png", "public.tiff"}:
            clip_type = "image"
        else:
            clip_type = "other"
    _emit({
        "type": "clipboard",
        "app": app_name,
        "content": text,
        "clip_type": clip_type,
        "ts": _now_iso(),
    })


def _on_activate(app_name: str, pid: int) -> None:
    title, doc_path = _focused_window(pid)
    payload: dict = {
        "type": "window",
        "app": app_name,
        "window": title,
        "pid": pid,
        "ts": _now_iso(),
        "idle_seconds": _idle_seconds(),
    }
    url = _browser_url(app_name)
    if url:
        payload["url"] = url
    elif doc_path:
        payload["file_path"] = doc_path
    _emit(payload)
    _sample_clipboard(app_name)


def main() -> int:
    try:
        from ApplicationServices import AXIsProcessTrusted
        from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification
        from Foundation import NSDate, NSObject, NSRunLoop
    except ImportError as e:
        _emit({"type": "fatal", "reason": f"pyobjc missing: {e!r}"})
        return 3
    if not AXIsProcessTrusted():
        _emit({"type": "fatal", "reason": "ax_untrusted"})
        return 3

    class _Observer(NSObject):
        def activated_(self, note):
            try:
                app = (note.userInfo() or {}).get("NSWorkspaceApplicationKey")
                if app is None:
                    return
                _on_activate(
                    str(app.localizedName() or ""), int(app.processIdentifier())
                )
            except Exception as e:  # never let an exception cross into ObjC
                print(f"[cowork-sidecar] observer error: {e!r}", file=sys.stderr, flush=True)

    observer = _Observer.alloc().init()
    center = NSWorkspace.sharedWorkspace().notificationCenter()
    center.addObserver_selector_name_object_(
        observer, "activated:", NSWorkspaceDidActivateApplicationNotification, None
    )
    _emit({"type": "ready", "ts": _now_iso()})
    rl = NSRunLoop.currentRunLoop()
    try:
        while True:
            # runUntilDate_ blocks until an event or the slice ends; the guard
            # below only matters if a live observer makes it return instantly
            # (the calendar driver's busy-spin lesson).
            t0 = time.monotonic()
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))
            elapsed = time.monotonic() - t0
            if elapsed < 0.05:
                time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        center.removeObserver_(observer)


if __name__ == "__main__":
    sys.exit(main())
