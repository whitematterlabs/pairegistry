#!/usr/bin/env python3
"""browse subagent entrypoint.

Invoked once per spawn. Builds a browser-use Agent with the parent's
resolved provider/model, runs the task, writes /proc/$PAI_SLUG/result.md.
The verbose agent loop lives inside this subprocess — the parent PAI's
context only ever sees the final result file.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib import request as _urlreq
from urllib.parse import urlparse

import yaml

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
LIBEXEC = PAI_ROOT / "usr" / "libexec" / "subagents" / "browse"
COOKIES_DIR = PAI_ROOT / "var" / "lib" / "browse" / "cookies"
COOKIE_TTL = 24 * 3600

# Hosts whose WAFs reliably block headless Chromium. Auto-route to CDP attach.
WAF_HOSTS = {
    "opentable.com", "resy.com", "exploretock.com", "tock.com",
    "yelp.com", "sevenrooms.com",
    "www.google.com",
}

CHROME_CDP_DEFAULT_PORT = 9222

sys.path.insert(0, str(LIBEXEC / "vendor"))


def _slug() -> str:
    s = os.environ.get("PAI_SLUG")
    if not s:
        sys.exit("entry.py: $PAI_SLUG not set")
    return s


def _workspace(slug: str) -> Path:
    return PAI_ROOT / "proc" / slug


def _spec(slug: str) -> dict:
    spec_path = _workspace(slug) / "spec.yaml"
    if not spec_path.is_file():
        sys.exit(f"entry.py: {spec_path} not found")
    with spec_path.open() as f:
        return yaml.safe_load(f) or {}


def _ensure_cookies(profile: str) -> Path | None:
    if not profile:
        return None
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    target = COOKIES_DIR / f"{profile}.txt"
    fresh = target.is_file() and (time.time() - target.stat().st_mtime) < COOKIE_TTL
    if not fresh:
        importer = LIBEXEC / "chrome_cookies_import.py"
        py = LIBEXEC / "venv" / "bin" / "python"
        subprocess.run(
            [str(py), str(importer), "--profile", profile],
            check=True,
        )
    return target if target.is_file() else None


def _build_llm(provider: str, model: str):
    p = (provider or "").lower()
    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model)
    if p == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model)
    if p in ("deepseek", "groq", "openrouter"):
        from langchain_openai import ChatOpenAI
        base = {
            "deepseek": "https://api.deepseek.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }[p]
        key_env = {
            "deepseek": "DEEPSEEK_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }[p]
        return ChatOpenAI(model=model, base_url=base, api_key=os.environ.get(key_env))
    sys.exit(f"entry.py: unsupported provider {provider!r}")


def _cdp_alive(url: str) -> bool:
    try:
        with _urlreq.urlopen(url.rstrip("/") + "/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_REAL_PROFILE = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
CHROME_CDP_PROFILE = PAI_ROOT / "var" / "lib" / "browse" / "chrome-cdp-profile"


def _quit_chrome() -> None:
    """Best-effort quit of any running Chrome so we can take over the profile."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "Google Chrome"], capture_output=True, text=True
        )
        if not out.stdout.strip():
            return
    except Exception:
        return
    subprocess.run(
        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
        capture_output=True,
    )
    for _ in range(20):
        r = subprocess.run(["pgrep", "-f", "Google Chrome"], capture_output=True)
        if not r.stdout.strip():
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-f", "Google Chrome"], capture_output=True)
    time.sleep(1)


def _prepare_cdp_profile() -> Path:
    """Create our CDP user-data-dir with symlinks into the owner's real Default
    profile, so cookies/TLS state/IP reputation transfer. Idempotent."""
    CHROME_CDP_PROFILE.mkdir(parents=True, exist_ok=True)
    real_default = CHROME_REAL_PROFILE / "Default"
    real_local_state = CHROME_REAL_PROFILE / "Local State"
    link_default = CHROME_CDP_PROFILE / "Default"
    link_local_state = CHROME_CDP_PROFILE / "Local State"
    if real_default.exists() and not link_default.exists():
        link_default.symlink_to(real_default)
    if real_local_state.exists() and not link_local_state.exists():
        link_local_state.symlink_to(real_local_state)
    return CHROME_CDP_PROFILE


def _ensure_chrome_cdp(port: int = CHROME_CDP_DEFAULT_PORT) -> str:
    """Ensure the owner's real Chrome is running with CDP. Returns the http:// URL.

    No-op if Chrome is already up on the port (subsequent browse spawns reuse it).
    Otherwise launches Chrome ourselves with:
      - --user-data-dir pointing at our own dir whose Default is a symlink to
        the owner's real Default profile (so cookies + Local State carry over).
      - NO --restore-last-session: Chrome opens a single about:blank tab. The
        previous chrome-cdp script enabled session restore, which raced with
        browser-use's tab management and caused tabs to flicker open/close.
    """
    url = f"http://127.0.0.1:{port}"
    if _cdp_alive(url):
        return url

    if not CHROME_APP.is_file():
        sys.exit(f"entry.py: Chrome not found at {CHROME_APP}")

    _quit_chrome()
    profile = _prepare_cdp_profile()

    subprocess.Popen(
        [
            str(CHROME_APP),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins=http://127.0.0.1:{port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if _cdp_alive(url):
            return url
        time.sleep(0.5)
    sys.exit(f"entry.py: Chrome did not expose CDP on {url} within 30s")


async def _run(
    task: str,
    url: str,
    headless: bool,
    cookies_file: Path | None,
    llm,
    cdp_url: str | None,
):
    from browser_use import Agent
    from browser_use.browser import Browser, BrowserConfig

    if cdp_url:
        browser = Browser(config=BrowserConfig(cdp_url=cdp_url))
    else:
        browser = Browser(config=BrowserConfig(
            headless=headless,
            cookies_file=str(cookies_file) if cookies_file else None,
        ))
    full_task = f"Start at {url}. {task}"
    agent = Agent(task=full_task, llm=llm, browser=browser)
    history = await agent.run()
    try:
        if cdp_url:
            # Don't quit the owner's Chrome; only drop the Playwright connection.
            disconnect = getattr(browser, "disconnect", None)
            if callable(disconnect):
                maybe = disconnect()
                if asyncio.iscoroutine(maybe):
                    await maybe
            else:
                await browser.close()
        else:
            await browser.close()
    except Exception:
        pass
    return history


def _summarize(history) -> str:
    parts: list[str] = []
    final = getattr(history, "final_result", None)
    if callable(final):
        try:
            parts.append(str(final()))
        except Exception:
            pass
    urls_fn = getattr(history, "urls", None)
    if callable(urls_fn):
        try:
            visited = list(urls_fn())
            if visited:
                parts.append("Visited:\n" + "\n".join(f"- {u}" for u in visited))
        except Exception:
            pass
    if not parts:
        parts.append(repr(history))
    return "\n\n".join(parts)


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "yes", "y", "on")


def _registrable(host: str) -> str:
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def _waf_match(host: str) -> bool:
    h = (host or "").lower()
    if h in WAF_HOSTS:
        return True
    parent = _registrable(h)
    return parent in WAF_HOSTS


WAF_MARKERS = (
    "access denied",
    "pardon our interruption",
    "are you a robot",
    "unusual traffic",
)


def _detect_waf_block(history) -> bool:
    """Heuristic: did the run hit a WAF wall? Best-effort, never raises."""
    try:
        text_blob = ""
        for attr in ("extracted_content", "model_outputs", "errors"):
            fn = getattr(history, attr, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        text_blob += "\n" + "\n".join(str(x) for x in (val or []))
                except Exception:
                    pass
        text_blob += "\n" + repr(history)
        low = text_blob.lower()
        if any(m in low for m in WAF_MARKERS):
            return True
        if "err_http2_protocol_error" in low or " 503" in low:
            return True
        if low.count("recaptcha") >= 3:
            return True
    except Exception:
        return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--headless", default="true")
    ap.add_argument("--profile", default="")
    ap.add_argument("--cdp", default="", help="Attach to running Chrome at this CDP URL")
    ap.add_argument(
        "--cdp-auto",
        default="false",
        help="Auto-launch the owner's Chrome via chrome-cdp and attach over CDP",
    )
    args = ap.parse_args()

    slug = _slug()
    ws = _workspace(slug)
    ws.mkdir(parents=True, exist_ok=True)
    result_path = ws / "result.md"

    cdp_url = args.cdp.strip() or None
    cdp_auto = _truthy(args.cdp_auto)
    auto_routed = False

    # Auto-route WAF-protected hosts to CDP attach when the caller didn't
    # already request CDP.
    if not cdp_url and not cdp_auto:
        try:
            host = (urlparse(args.url).hostname or "").lower()
        except Exception:
            host = ""
        if host and _waf_match(host):
            cdp_auto = True
            auto_routed = host

    try:
        spec = _spec(slug)
        provider = spec.get("provider")
        model = spec.get("model")
        if not provider or not model:
            sys.exit("entry.py: spec.yaml missing provider/model")

        if cdp_auto and not cdp_url:
            cdp_url = _ensure_chrome_cdp()

        cookies_file = None if cdp_url else _ensure_cookies(args.profile.strip())
        llm = _build_llm(provider, model)
        history = asyncio.run(
            _run(args.task, args.url, _truthy(args.headless), cookies_file, llm, cdp_url)
        )
        body = _summarize(history)

        mode_line = (
            f"- mode: cdp-attach (endpoint={cdp_url})\n"
            if cdp_url
            else f"- mode: bundled-chromium (headless={args.headless})\n"
        )
        auto_line = (
            f"- auto-routed to CDP mode (host {auto_routed} matched WAF allowlist)\n"
            if auto_routed
            else ""
        )

        # WAF detection. If we got blocked even in CDP mode, escalate distinctly
        # so the parent doesn't loop trying the same fix.
        blocked = _detect_waf_block(history)
        if blocked:
            try:
                host = (urlparse(args.url).hostname or "").lower()
            except Exception:
                host = ""
            marker = "WAF_BLOCKED_CDP" if cdp_url else "WAF_BLOCKED"
            result_path.write_text(
                f"# browse result\n\n"
                f"{marker}: {host}\n\n"
                f"- task: {args.task}\n"
                f"- start url: {args.url}\n"
                f"{mode_line}{auto_line}"
                f"- profile: {args.profile or '(none)'}\n\n"
                f"## outcome\n\n{body}\n"
            )
            return 2

        result_path.write_text(
            f"# browse result\n\n"
            f"- task: {args.task}\n"
            f"- start url: {args.url}\n"
            f"{mode_line}{auto_line}"
            f"- profile: {args.profile or '(none)'}\n\n"
            f"## outcome\n\n{body}\n"
        )
        return 0
    except SystemExit:
        raise
    except Exception:
        result_path.write_text(
            f"# browse result (error)\n\n```\n{traceback.format_exc()}\n```\n"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
