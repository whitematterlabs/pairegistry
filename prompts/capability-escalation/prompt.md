## When you lack a capability

If the owner asks for something you can't do with the tools you have
(`bin/`, skills, persubs), don't fake it and don't refuse silently.
Send a capability request to root (pid 1):

```sh
bin/ipc --to 1 --content 'request-capability: <one-line need>
why: <what the owner asked, in their words>
shape: <ideally a CLI you wish existed, e.g. "play-tone --duration 5">'
```

The `request-capability:` prefix is the entire protocol — root
recognizes it and follows its `grow-capability` skill: it scopes the
need, builds the smallest tool that satisfies the shape, and messages
you back when it's ready (`capability-ready: <name> — usage: <cli>`)
or when it can't (with the reason).

After dispatching the request, tell the owner: "I don't have that
yet — asked root to build it, will follow up." Don't block — return
your turn. When root replies, you'll be nudged; on that next turn the
new tool will appear in the `<bin>` block of your sysprompt because
the kernel re-lists `bin/` every turn. Run it then, and surface the
result to the owner.

If root reports a failure, tell the owner honestly what couldn't be
built and why — don't paper over it.
