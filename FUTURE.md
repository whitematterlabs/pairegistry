# FUTURE

Items deferred behind telemetry or other observed need. Don't build until the data says we should.

## Subagent kernel features

- **Subagent depth caps** — only matters if a coder spawns a coder (or scout spawns either). Today's prompts forbid it; verify via `/sys/subagents/<slug>/metrics.yaml` (step 4 telemetry) before building enforcement. Revisit ~2026-06.
- **Context compaction for subagents** — only matters once a coder hits context limits inside a long `claude -p` chain. Premature without `claude_p_invocations` telemetry showing the failure mode. Revisit ~2026-06.
