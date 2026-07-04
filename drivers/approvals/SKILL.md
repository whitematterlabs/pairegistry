---
name: approvals
description: Draft-and-approve — outbound sends under a capability in `ask` mode are queued automatically for the owner to approve, no different action from you.
driver: approvals
---

# Draft & approve

When a send capability is in **`ask`** mode (see the `<capabilities>` block), you
send exactly as you normally would — `action: send` on an email draft, a bare
line to an iMessage thread. The outbound driver detects the capability is
gated and automatically stages your attempted send into the owner's approval
queue instead of delivering it. You never call a separate tool for this.

## When to use

You decided to send something (an email reply, a follow-up) and the
`<capabilities>` block says **APPROVAL REQUIRED** for that channel. Send
normally. Tell the owner you sent it for approval — never that it was
delivered outright; the driver, not you, is what queues it.

## Lifecycle

`pending` → owner approves in the web console → `approved` → the approvals
driver delivers it → `dispatched` (email hands off to the mature macmail send
path) or `sent` (iMessage is delivered inline by this driver). Owner rejects →
`rejected`, and you'll see the same failure signal you'd get from any other
failed send (`email:draft_failed` / `imessage:send_failed`) — no new event
kind to learn. The driver emits `approvals:pending` when a send is queued (so
the owner is badged) and `approvals:dispatched|sent|rejected|failed` on the
outcome.

## Pitfalls

- Don't claim a message was sent. In `ask` mode it is only *queued* until the
  owner acts.
- The owner can edit the body before approving — what actually goes out may
  differ slightly from what you sent.
- Carries **email** and **imessage**.

## Verification

Check `email:draft_failed` / `imessage:send_failed` for a rejection reason, or
`draft_state: pending_approval` on the original email draft yaml while it
waits. The message leaves only after the owner approves it.
