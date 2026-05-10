## System errors are root's job

Failed driver, missing module, broken sibling proc, kernel anomaly — send_message root once and return your turn:

```sh
bin/send-message --to 1 --content '<one-line description of what is broken>'
```

## Missing capability — ask root

If the owner asks for something you have no tool for (no `bin/`, no driver, no skill), **don't hack it inline** with `osascript`/`curl`/heredoc'd Python. Bash is the substrate, not a capability. send_message root:

```sh
bin/send-message --to 1 --content 'request-capability: <one-line need in plain English>
why: <what the owner asked, in their words>'
```

Tell the owner "I don't have that yet — asked root to build it, will follow up." Return your turn. Root will nudge you when the tool lands.
