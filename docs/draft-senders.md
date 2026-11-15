# Native draft senders: installed official MCP operations

This document describes the current local draft sender candidate. The v2 review and subsequent native placement, edit-payload and completion-state reviews are closed; the v2 historical freeze remains in `testing/draft-feature-frozen-v2/sha256.json`, while later fixes must be read from the final source manifest. Phase17 exact-bundle Linux validation completed with 231 tests, 230 passing and one explicit source-only skip. The final release manifest and external validation record identify the deployed server and APK; these protocol notes do not imply a later candidate was deployed. Actual Android Edit/Send was exercised against a loopback MCP mock; no real public issue, comment, email or SMS was sent.

The domain and retained Android card/action schemas are documented in [DRAFT_CARDS_PROTOCOL.md](DRAFT_CARDS_PROTOCOL.md). This file describes the production MCP adapters, their discovery and receipt rules. The generic domain supports trusted sender extensions; a widget kind alone never configures a delivery service.

## Registration and available senders

`server.py` registers connectors and rules, then `register_draft_senders(aibo, tasks, engine, manager)`. `NativeEngine.current_extensions()` synchronizes adapters before a new tool snapshot; a model turn snapshots definitions and handlers together. Persisted actionable cards can also request sender synchronization after restart; a same persisted credential/schema/permission identity can be rebound, while a changed version revokes the old card's sender identity.

Only a currently connected, non-readonly installation from the reviewed GitHub catalog can supply these adapters. The actual remote tool must be `issue_write` or `add_issue_comment`, and its discovered input schema must fit the reviewed operation. The checker requires an object with supported property types and no unsupported required fields; references, combinators, conditional/dependent schemas and incompatible types are refused. `issue_write` must permit method `create`. Unknown GitHub tools and schema drift are not advertised as draft senders.

Sender IDs retain the installation namespace, for example `mcp__installation_id__issue_write`. The immutable binding ID is derived from the connector tool version, not a model-selected account name. Separate installations remain separate accounts, versions and permissions even if they explicitly share an OAuth grant.

`list_message_draft_senders({})` returns permitted sender metadata without a network call or write. It refuses private planning runs and only returns bindings compatible with the current turn snapshot. `create_message_draft` produces an owned visible draft; it never calls a remote sender. A model cannot replace these adapters with an arbitrary tool name, hidden arguments, credential or account.

## Exact visible-field mapping

Both implemented GitHub adapters use the native widget's `kind="other"`.

| Visible card field | Create issue | Add issue/PR comment |
| --- | --- | --- |
| `from_account` | Exact `GitHub / <installation ID>` | Same exact installation label |
| `to` | `owner/repository` | `owner/repository#positive_number` |
| Selected `subject` | Required nonempty issue title | Must be absent or empty |
| Selected `body` | Required nonempty issue body | Required nonempty comment body |
| Fixed operation | `issue_write`, `method="create"` | `add_issue_comment`, parsed `issue_number` |

Only these displayed words and parsed target become MCP arguments. No labels, assignees, reactions, attachments, alternate credentials or hidden repository actions are synthesized. Comment targets may be issues or PRs because the official operation supports both; this does not authorize review-comment operations. Each variant is validated before the card becomes actionable and the selected payload is revalidated at dispatch.

The domain can represent `email` and `textMessage` widgets and accepts explicitly registered trusted adapters. **No production email or SMS sender is configured by this implementation.** These kinds do not prove mail/SMS delivery. The user's broader scope permits official MCP replacements and skipping services without official MCP support; the currently reviewed GitHub adapters/catalog are an implementation boundary, not a permanent decision to support only three services.

## Native Send and dispatch authority

The user's explicit native Send action is the approval for the exact selected draft words, recipient, installation and revision. Listing senders, model proposal, draft creation and edited-draft commentary are not execution approval. Ordinary model calls to MCP tools still use the engine's normal approval path; the draft mechanism does not make the MCP tool globally approved.

Before reserving and dispatching, the domain checks account/channel ownership, source session, selected variant/revision, sender identity and current task lifecycle/binding. The adapter checks connector permissions and standing rules: unavailable/blocked connectors or Never rules prevent dispatch. The captured connector/rule decision and payload authorization are checked again after asynchronous OAuth/transport preparation and immediately before sending through the manager's `dispatch_guard`. Changing credentials, schema, connector permissions, task state or approved content cannot silently retarget an old approval.

Task drafts use their own persisted decision-card UUID and exact task/card/message relationship. They do not use a parent Working notice as authority. The specialized task-list rendering depends on these six flags together: `tasks_lists`, `tasks_card`, `tasks_chat_stack`, `tasks_waiting_draft`, `tasks_chat_stack_approve`, `tasks_chat_stack_approve_send`. Retained native dependency evidence is `llx.smali:1405` and `cx5.smali:2732`. The four edit capabilities are `card_draft_edits`, `card_draft_edit_words`, `card_draft_edits_reply`, `card_draft_edit_words_reply_v2`. These are specific implemented dependencies, not permission to enable every draft/task capability. In particular, delayed Send-until-pause and paired send-call representations remain disabled as described in the domain protocol.

## Receipts, forms and retry boundaries

A non-error MCP response is not evidence of delivery. Success requires a parseable structured result (or one JSON text content part), a positive numeric remote ID and a verified HTTPS `github.com` target URL for the approved owner/repository. New issues require an issue URL and, when wrapped, `method="create"`. Comments require the exact approved issue/PR number and `#issuecomment-<remote ID>`. A generic success string, absent ID, mismatched repository/issue/fragment or ordinary error is not marked sent. Only the bounded `tool,remote_id,url,operation_id` receipt survives; raw upstream output is not treated as an authored message.

The official GitHub server can return an interactive form with `status="awaiting_user_submission"`: the external operation has not been performed. The exact primary source was fetched at [github-mcp-server commit 0663b830, pkg/github/issues.go](https://github.com/github/github-mcp-server/blob/0663b830582af923e30fb14cb2e10dd3248baa86/pkg/github/issues.go), retained locally as `work/reference/github-issues-0663b830.go`. It deliberately marks this form result `isError=true`.

The adapter recognizes only the known narrowly structured issue-create awaiting-form shape before applying generic error handling; this produces `DraftSendFailed`, a certified no-effect result. It does not interpret every error, arbitrary `awaiting` field or user-submission status from another tool as proof of no side effect. An explicit retry of a certified no-effect attempt must match the durable approved payload, whole-card hash, source/accepted revisions, sender and task bindings; it dispatches stored words and cannot replay old edits onto a newer card. A new explicit Send can instead approve the current revision. Legacy failed attempts lacking the required durable bindings fail closed.

All other exceptions, cancellation, malformed/unverified results and lost success persistence remain unknown outcomes and cannot automatically replay that variant. A known external write with an uncommitted receipt is still unknown, not success. Restarted pending work becomes unknown. Exact repeated historical requests may read their already-authored acceptance receipt after authorization and original reference checks, including when the task decision has since completed; this lookup does not execute another send.

## Lifecycle and evidence

Accepting Send persists an authored user action and returns its message ID before the background delivery finishes. Acceptance is not delivery. Only a verified successful adapter result updates the selected variant's native `server_saw_sent` marker. Live jobs also occupy the engine active-task registry as `draft_send:<operation_id>`, so health, maintenance and shutdown observe them. Maintenance rejects new dispatches before reservation, while exact historical receipts remain readable. Channel deletion checks draft idleness before mutating any store, blocks active delivery and cleans only the owned closed records, with checkpoint rollback on failure.

The current scoped draft-domain/sender suite passed 74 checks. These supersede earlier overlapping 50-case/health checkpoints as the reported affected-suite result; they are not a full final-server test total. Independent review closed stale retry/new-body, terminal-task historical dedup, restart sender, native placement, strict edited-body parsing and atomic completion-state issues. The awaiting-form fixture includes the official `isError=true` shape.

Actual fresh main-chat native cards displayed two variants; selecting version 2 and Send produced one loopback write with its unedited words. A separate native Edit/Send submitted the exact edited Chinese body of version 1, producing one additional loopback write. The original UI showed sent status and the pending spinner ended. Retained XML/screens are pixel 334–347 in the private audit evidence; they are not distributed. Older cards becoming read-only after a later user turn follows the retained native policy. These checks establish the actual native flow to a synthetic MCP endpoint, not real GitHub delivery.

Edited Send accepts the strict native qf6 body-only edit form under the bound card ownership/revision. Completion message/state is committed atomically. The Tasks shortcut projection is limited to one eligible EMAIL variant without explicit from-account; a generic domain fixture does not establish a production email sender. Gmail's separate official MCP connector can save drafts only and is not registered here.

Real provider OAuth/write delivery remains outside the recorded mock-send evidence. Phone restoration, cleanup, later bundle validation and deployment are tracked in the external release verification record. Local loopback success does not establish full original-feature parity.
