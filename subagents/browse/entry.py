#!/usr/bin/env python3
"""browse subagent entrypoint.

Always drives the owner's real Chrome over CDP. No bundled Chromium,
no separate profile — the only mode is "take over the user's Chrome,
use their real cookies/sessions, drive it via browser-use."
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

CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_REAL_PROFILE = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
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


def _quit_chrome() -> None:
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


def _ensure_chrome_cdp(port: int = CHROME_CDP_DEFAULT_PORT) -> str:
    """Ensure the owner's real Chrome (on their real Default profile) is
    running with CDP. Returns the http:// URL.

    No-op if Chrome is already up on the port (subsequent browse spawns
    reuse it). Otherwise quits any running Chrome and relaunches it
    against ``CHROME_REAL_PROFILE`` with --remote-debugging-port. Quitting
    first is the SQLite-corruption guard: two Chromes writing the same
    profile dir will trash cookies/Local State.
    """
    url = f"http://127.0.0.1:{port}"
    if _cdp_alive(url):
        return url

    if not CHROME_APP.is_file():
        sys.exit(f"entry.py: Chrome not found at {CHROME_APP}")
    if not CHROME_REAL_PROFILE.is_dir():
        sys.exit(f"entry.py: Chrome profile not found at {CHROME_REAL_PROFILE}")

    _quit_chrome()

    subprocess.Popen(
        [
            str(CHROME_APP),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins=http://127.0.0.1:{port}",
            f"--user-data-dir={CHROME_REAL_PROFILE}",
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


async def _run(task: str, url: str, llm, cdp_url: str):
    from browser_use import Agent, Browser, BrowserConfig

    browser = Browser(config=BrowserConfig(cdp_url=cdp_url))
    full_task = f"Start at {url}. {task}"
    agent = Agent(task=full_task, llm=llm, browser=browser)
    history = await agent.run()
    try:
        disconnect = getattr(browser, "disconnect", None)
        if callable(disconnect):
            maybe = disconnect()
            if asyncio.iscoroutine(maybe):
                await maybe
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


WAF_MARKERS = (
    "access denied",
    "pardon our interruption",
    "are you a robot",
    "unusual traffic",
)


def _detect_waf_block(history) -> bool:
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
    ap.add_argument(
        "--cdp",
        default="",
        help="Override CDP endpoint (e.g. http://127.0.0.1:9223). "
        "Default: take over the owner's Chrome on port 9222.",
    )
    args = ap.parse_args()

    slug = _slug()
    ws = _workspace(slug)
    ws.mkdir(parents=True, exist_ok=True)
    result_path = ws / "result.md"

    try:
        spec = _spec(slug)
        provider = spec.get("provider")
        model = spec.get("model")
        if not provider or not model:
            sys.exit("entry.py: spec.yaml missing provider/model")

        cdp_url = args.cdp.strip() or _ensure_chrome_cdp()
        llm = _build_llm(provider, model)
        history = asyncio.run(_run(args.task, args.url, llm, cdp_url))
        body = _summarize(history)

        mode_line = f"- mode: cdp-attach (endpoint={cdp_url})\n"

        if _detect_waf_block(history):
            try:
                host = (urlparse(args.url).hostname or "").lower()
            except Exception:
                host = ""
            result_path.write_text(
                f"# browse result\n\n"
                f"WAF_BLOCKED: {host}\n\n"
                f"- task: {args.task}\n"
                f"- start url: {args.url}\n"
                f"{mode_line}\n"
                f"## outcome\n\n{body}\n"
            )
            return 2

        result_path.write_text(
            f"# browse result\n\n"
            f"- task: {args.task}\n"
            f"- start url: {args.url}\n"
            f"{mode_line}\n"
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
