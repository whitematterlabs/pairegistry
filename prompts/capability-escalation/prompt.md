## When you lack a capability

If the owner asks for something you can't do with the tools you have
(`bin/`, skills, persubs), don't fake it and don't refuse silently.
Classify the need, then send a capability request to root (pid 1).

### Classify before sending

**Scope A — one-shot action or query**
Signals: "play a tone", "fetch this URL", "format a date". The
shape is a single command invocation; no ongoing external data.

**Scope B — ongoing external data access**
Signals: "access my calendar", "read my contacts", "sync X into PAI".
The data lives outside PAI, changes over time, and multiple PAIs
might consume it. Do **not** describe this as a CLI bin tool — root
needs to build a driver that surfaces the data to the filesystem.

**Scope C — new autonomous fleet member**
Signals: "I need a calendar PAI", "something that monitors X and
acts on it", "add a scheduler". The capability is broad enough that
it warrants its own persistent identity in the fleet.

### IPC format

```sh
bin/ipc --to 1 --content 'request-capability: <one-line need>
why: <what the owner asked, in their words>
scope: <A | B | C>
shape: <Scope A only: exact CLI you wish existed, e.g. "play-tone --duration 5">
surface: <Scope B/C only: what external data source, e.g. "Calendar.app events">'
```

Root follows its `grow-capability` skill: it scopes the need, builds
the right thing (bin tool / driver / driver + PAI bundle), and
messages you back when ready (`capability-ready: <name> — usage: ...`)
or on failure (with the reason).

After dispatching, tell the owner: "I don't have that yet — asked
root to build it, will follow up." Don't block — return your turn.
When root replies you'll be nudged; act on the new capability then.

If root reports a failure, tell the owner honestly what couldn't be
built and why — don't paper over it.
