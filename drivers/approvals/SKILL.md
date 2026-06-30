---
name: approvals
description: Draft-and-approve — propose an outbound send and let the owner approve it before it leaves.
driver: approvals
---

# Draft & approve

When a send capability is in **`approve`** mode (see the `<capabilities>` block),
you may **propose** a send but never send it yourself. You queue it; the owner
approves it in the web console; the approvals driver delivers it.

## When to use

You decided to send something (an email reply, a follow-up) and the
`<capabilities>` block says **APPROVAL REQUIRED** for that channel. Do not write
`action: send` and do not append a bare iMessage line — both are frozen and
won't leave. Queue the action instead.

## Procedure

1. Compose the full message as you normally would.
2. Queue it with `propose-send`:
   ```
   propose-send --channel email \
     --from <your-account> --to <recipient> --subject "<subject>" \
     --in-reply-to "<message-id>"   # for a reply; omit --to is fine for replies
     --summary "<one line the owner sees in the queue>" \
     --source-event email:new --source-ref <inbound path> \
     --body -    # body on stdin
   ```
   It writes one record to `var/spool/approvals/` and prints its id. Nothing is
   sent.
3. Tell the owner you **queued it for approval** — never that it was sent.

## Lifecycle

`pending` → owner approves in the web console → `approved` → the approvals
driver delivers it → `dispatched` (email hands off to the mature macmail send
path) / `sent`. Owner rejects → `rejected`. The driver emits `approvals:pending`
when you queue one (so the owner is badged) and `approvals:dispatched|sent|failed`
on the outcome.

## Pitfalls

- **Never** set `action: send` yourself in approve mode — it's frozen and saved
  as a Mail.app draft, not sent.
- Don't claim a message was sent. In approve mode it is only *queued* until the
  owner acts.
- v1 carries **email**. iMessage proposes land once the iMessage adapter ships.

## Verification

`propose-send` prints `queued for approval: <id>` and the record path. The owner
sees it in the web console's approval tray; the message leaves only after they
approve.
