# Engineering contract

- Keep domain records and deterministic approval rules free of network/GUI dependencies.
- Adapters implement narrow ports. Do not import an upstream bot's automatic reply loop.
- WeChat gateway metadata, credentials, queues and logs belong to the mandatory workspace, under `.wechat-assistant`. Owner-received documents go directly to the selected Codex client's primary local project directory. Native Codex login, sessions and memories remain in the host's official Codex state directory.
- Never copy global authentication files into a workspace. Use official scoped login.
- Only authenticated owner requests authorize answers back to that owner's ClawBot chat.
- No WeChat contact monitoring, contact replies, approval workflows or GUI adapters.
- File sends require an exact owner command bound to project, hash, body and destination.
- One transport writer. Do not retry uncertain writes without explicit operator reconciliation.
- A quote without a matching message ID or complete random marker is unresolved.
- Preserve raw source records. Summaries must not promote unsent drafts into facts.
- File receipt means saving to the selected local project directory; cloud upload is not a prerequisite.
- Never claim local project files automatically share ChatGPT cloud memory or cloud Sources.
- Only authenticated owner requests may trigger official local Codex CLI execution in the selected native local project's primary directory. Reuse the host's normal Codex state and exact thread IDs; do not synthesize memory from local delivery history. If Codex login or the project mapping is unavailable, report it and retain transfer records without model execution. Do not reintroduce scheduled native relay or another model backend.
- Test concurrency, restart, evidence invalidation, quote matching and echo suppression.
- Do not test against real contacts or change login/UI state without the operator's current instruction.
- Report simulated, compiled and live-verified capabilities separately.
