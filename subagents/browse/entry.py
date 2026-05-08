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

import yaml

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
LIBEXEC = PAI_ROOT / "usr" / "libexec" / "subagents" / "browse"
COOKIES_DIR = PAI_ROOT / "var" / "lib" / "browse" / "cookies"
COOKIE_TTL = 24 * 3600

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


async def _run(task: str, url: str, headless: bool, cookies_file: Path | None, llm):
    from browser_use import Agent
    from browser_use.browser import Browser, BrowserConfig

    browser = Browser(config=BrowserConfig(
        headless=headless,
        cookies_file=str(cookies_file) if cookies_file else None,
    ))
    full_task = f"Start at {url}. {task}"
    agent = Agent(task=full_task, llm=llm, browser=browser)
    history = await agent.run()
    try:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--headless", default="true")
    ap.add_argument("--profile", default="")
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

        cookies_file = _ensure_cookies(args.profile.strip())
        llm = _build_llm(provider, model)
        history = asyncio.run(
            _run(args.task, args.url, _truthy(args.headless), cookies_file, llm)
        )
        body = _summarize(history)
        result_path.write_text(
            f"# browse result\n\n"
            f"- task: {args.task}\n"
            f"- start url: {args.url}\n"
            f"- headless: {args.headless}\n"
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
