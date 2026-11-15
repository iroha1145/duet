# Native user questions

This describes the server protocol. Device/UI compatibility requires separate verification. See [Plan mode](../docs/PLAN-MODE-COMPATIBILITY.md).

## Execution and authority

`EngineService.tool_definitions` exposes `AskUserQuestion` when tools are enabled.
Its input is `questions`, each with `question`, `header`, `options` (label and
optional description), and optional `multiSelect` (default false). The model
cannot supply `answers`. Inputs and answers are bounded to 16 KiB each; question
texts and option labels must be distinct, and text cannot be blank. Optional
newer SDK preview/annotation/metadata features are not implemented here.

The existing engine loop waits on its existing `request_approval` future, with
an explicit `QuestionAnswer` response type. Ordinary execution approvals still
use booleans. The same model tool-call ID receives
`{"questions": <original questions>, "answers": {<question text>: <user words>}}`
and the loop continues. Declining produces a tool error rather than invented
answers. Auto-approve, standing rules, and model-generated input cannot answer
for the user. No alternate execution engine or fixture-only registration exists.

The native card response uses `question` for one question, or
`questions: {entries: [...]}` for multiple questions. Every question has
`options: [{index, label, description?}]`, `pick_several`, `takes_other`, `header`,
and `question`. Explicit false `can_pick`/`can_pick_questions` capabilities are
respected. The card uses the neutral `Answer questions` title in English and
`请回答问题` when the project language starts with `zh`.

The original pick request is:

```json
{
  "session_id": "session_...",
  "request_id": "...",
  "token": "...",
  "answer": "pick",
  "option_index": 0,
  "option_indexes": null,
  "other_text": null,
  "picks": null,
  "can_sign": null,
  "device_attestation": null
}
```

A single multi-select question uses `option_indexes`; a custom answer uses
`other_text`. Multiple questions use an ordered `picks` array of
`{option_indexes, other_text}`, leaving the top-level selection fields null.
An empty multi-question pick skips that question. At least one answer is needed
for `pick`; skipping everything uses the original decline path. Selected
indexes are sorted. Multi-select items containing `, ` or a double quote are
JSON-quoted before joining with `, `, matching the original client.

The second original path is a session `control_response` with `behavior: allow`
and `updatedInput: {questions: <unchanged originals>, answers: {...}}`.
`toolUseID`/`tool_name`/`toolName`, when present, must match the pending call.
Only a nonempty subset of the original question keys may appear in answers.
Missing keys represent skipped questions. This path does not support arbitrary
tool input editing or permission changes.

Both routes use Aibo's existing exact pending version, single operation, and
receipt. The native token binds account subject, session, request ID, original
pending/version, and expiry. A competing device joins the first operation;
replays do not replace its answer. A consumed answer with an unknown
acknowledgement is not resubmitted. Cancellation removes the waiting future.
Restart does not resume the old task, and fresh signing secrets invalidate old
native tokens. SDK requests also require a current session/request pairing.

The backend separately verifies the same pending future, session, tool name,
and unchanged questions before setting its typed result. Ordinary `approve`
cannot allow a question. In legacy-token mode, `/sessions/{id}/approve` also
rejects question `allow` before writing any session `always` grant; decline
remains possible. Account-login mode already disables legacy session routes.

The `read_file`/`list_files` DontAsk restriction is unchanged. Ask is not a
standing-rule scope. In this early Ask-only slice, `ExitPlanMode` and
`EnterPlanMode` were out of scope; the current integrated implementation and its
authorization limits are documented in
[`PLAN-MODE-COMPATIBILITY.md`](../docs/PLAN-MODE-COMPATIBILITY.md). Generic
execution/MCP parameters remain noneditable.

