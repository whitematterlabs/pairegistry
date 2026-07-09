---
name: using-slack
description: Read inbound Slack DMs and @-mentions, draft replies, handle send failures.
driver: slack
---

# Your Slack messages directory

Everything lives under `/home/<slug>/slack-messages/`. One folder per Slack
conversation — a person slug (or the raw Slack user id) for a DM, or
`#channel-name` for a channel you were @-mentioned in. Inside, one markdown
file per day.

```
/home/<slug>/slack-messages/<thread>/meta.yaml
/home/<slug>/slack-messages/<thread>/2026-07-09.md
```

A day-file is a flat log. Each line is `[HH:MM] <sender>: <text>`.

```
[13:28] Alper: can you check the deploy?
[13:30] me: on it
```

`me` = the owner (or you). `<DisplayName>` = inbound from them.

`meta.yaml` is maintained by the driver and carries the routing details a reply
needs — you never edit it:

```yaml
channel: slack
slack_channel: C0123ABCD      # the Slack conversation id
channel_type: channel         # "im" for a DM, "channel" for a mention
thread_ts: "1720500000.0012"  # channels only — the thread a reply goes into
```

You only wake on **DMs to you** and **@-mentions of you**. Plain channel chatter
with no mention is never recorded — you can't see it.

# How to do things

**Reply to a thread.** Append a **bare line** — just the message text, no
`[HH:MM] me:` prefix — to today's day-file. The driver picks up bare lines,
sends via `chat.postMessage` (threading a channel reply under the mention it
came from), then writes the canonical `[HH:MM] me: <text>` record itself.
Bracketed lines are log entries only and never get sent. Create today's file if
it doesn't exist.

```
echo "on it" >> /home/<slug>/slack-messages/<thread>/<today>.md
```

Whether you may send is decided by the owner's `slack_send` capability, stated
in your `<capabilities>` block. Always append the same bare line regardless of
mode:
- Send granted (`yes`) — delivered via `chat.postMessage` as described above.
- Approval required (`ask`) — the driver detects the gate and automatically
  queues your message in the owner's approval tray instead of sending (see the
  `approvals` skill); it appends a `kernel: queued for owner approval` note.
  Tell the owner you sent it for approval, not that it was delivered.
- Not granted (`no`, the default) — the driver consumes the line, appends a
  `kernel: send frozen` note, emits `slack-out:send_failed`, and does not reach
  Slack. The spool defaults to DENY.

If your `<capabilities>` block says Slack is read-only, don't attempt sends;
never retry a frozen or rejected send.

**Reply only to threads that already exist.** A Slack reply needs the
conversation id, which the driver only knows for a thread it received a message
in. A folder you create by hand has no `slack_channel` and is ignored — you
can't start a brand-new Slack conversation this way.

**Read a thread.** For a live `slack:new` event, read the event's `day_file`.
Otherwise read today's day-file first, then earlier days. Don't grep all
threads unless asked.

# Events you wake on

- **`slack:new`** — one inbound DM or @-mention. Payload has `thread`,
  `channel`, `channel_type`, `sender`, `text`, `thread_ts`, `day_file`. Read
  the day-file, then reply, surface, or stay silent (your role prompt decides).
- **`slack:backlog`** — slack-in just caught up on messages that arrived while
  it was down. Payload has `threads` with per-thread counts, `last_text`, and
  `day_files`. Read the listed `day_files`, then output a short recap — one
  bullet per thread that matters. Skip noise.
- **`slack-out:send_failed`** — your outbound didn't deliver: sends aren't
  granted, the thread had no resolvable channel, the API call failed, or the
  owner rejected a queued approval. Surface the thread, text, and reason to the
  owner. Don't retry.
