---
name: make-dashboards
description: Use when the owner asks for a live dashboard, chart, status board, or custom view in the web console — write a self-contained HTML artifact with an embedded manifest that renders as its own console tab and receives live data over postMessage.
---

# Making a console dashboard

You can add your own tab to the owner's web console. A dashboard is **one
self-contained HTML file** at `/var/lib/dashboards/<slug>.html`: your markup plus
inlined CSS/JS, with a small embedded manifest. The console discovers the file,
adds a tab, and — because your HTML runs in a hard **sandboxed iframe** — it
cannot fetch, cannot touch the console's session, and cannot reach the network.
The console is your **only** data source: it pushes the live channels you declare
into your frame as messages. You render them.

This is display-only. No build step, no toolchain — write the file and the tab
appears live.

## Write it with the `dashboard` bin

Don't hand-write the file; the bin validates the manifest and keeps it to one
greppable block. Pipe the HTML **body** on stdin; the manifest is built from the
flags:

```
dashboard write <slug> --title "Label" [--channel NAME]... [--order N] < body.html
dashboard list
dashboard remove <slug>
```

- `<slug>` — lowercase letters/digits/`.`/`_`/`-`, starts alphanumeric. It's the
  stable tab key; reuse it to update the same dashboard, don't mint a new one.
- `--title` — the tab label.
- `--channel` — a live-data stream to bridge in (repeatable). Omit if static.
- `--order` — tab sort (lower = earlier; default 100).

Any manifest already in the body is stripped, so exactly one is written.

## The HTML contract

Your body is plain HTML with inlined CSS/JS. Because of the sandbox CSP:

- **No network.** `fetch`, `XMLHttpRequest`, WebSocket, external `<script>`/`<link>`/
  `<img src=http…>` are all blocked. Inline everything. Images must be `data:` URIs.
- **Inline `<style>` and `<script>` work.** That's how you render.
- Charting must be inlined too (hand-rolled SVG/canvas, or a vendored library
  pasted inline). Keep it small.

## The data protocol (`pai:data`)

For each `--channel` you declared, the console `postMessage`s a frame into your
iframe **on load and again whenever that data changes** (event-driven, no
polling). Listen for it:

```html
<script>
  window.addEventListener("message", (e) => {
    if (e.data?.type !== "pai:data") return;
    const { channel, payload } = e.data;
    // channel is one of your declared channels; payload is its current value.
    render(channel, payload);
  });
</script>
```

You receive data; you never send anything back. Don't validate `e.origin` — the
frame is sandboxed to an opaque origin, so the parent posts with `"*"` and there
is no fixed origin to check. Only `pai:data` frames arrive.

## Channels available in v1

These bridge the live console state you can already see. Each `payload` is the
full current array (not a delta) — re-render from it each time.

| Channel | payload |
|---|---|
| `procs` | array of running processes: `{slug, pid, type, parent, description, status, busy, ctx_tokens, ctx_limit, when_short, …}` |
| `fleet` | array of running PAIs: `{pid, slug, fallback, clone_of, title}` |
| `drivers` | array of driver health: `{slug, driver, active, status, state, state_reason, starts, last_activity, …}` |
| `scheduled` | array of owner scheduled tasks: `{slug, pai, instruction, label, repeat, next_fire, …}` |

If you need data no channel exposes yet, say so — new channels are a backend
change, not something a dashboard can invent.

## Minimal example

```
dashboard write fleet-pulse --title "Fleet Pulse" --channel procs <<'HTML'
<!doctype html>
<body style="font:14px system-ui;margin:16px;color:#222">
  <h2>Running processes</h2>
  <ul id="list"><li>waiting for data…</li></ul>
  <script>
    addEventListener("message", (e) => {
      if (e.data?.type !== "pai:data" || e.data.channel !== "procs") return;
      const rows = e.data.payload || [];
      document.getElementById("list").innerHTML =
        rows.map(p => `<li>${p.slug} — ${p.status}${p.busy ? " (busy)" : ""}</li>`).join("")
        || "<li>none</li>";
    });
  </script>
</body>
HTML
```

The **Fleet Pulse** tab appears immediately and updates itself as processes come
and go. Delete it with `dashboard remove fleet-pulse` and the tab disappears live.

## Notes

- Dashboards live under `/var/lib/`, so they survive updates.
- Keep each dashboard focused — one board per concern. Many tabs scroll
  horizontally.
- The owner only *views* dashboards; they don't edit them. You own the content.
