# Original native task suggestions and Found offers

Implementation: `bridge/task_suggestions.py`, registered by `register_channel_tasks`.
The model tool is a local explicit contract. Native cards, task rows and REST actions follow the recovered original APK schemas; no original UI or APK code is replaced.

## Original evidence

Under `work/direct-inspect-network/sources/defpackage`:

- `nbr.java:13-18`, `obr.java:11-15`: conversation card `{kind:"task_suggestion",suggestion_id,origin_message_id,tasks:[{title,why}]}`.
- `mhb0.java:5-13`, `ln.java:144-146`: suggestion UUID and native origin message IDs.
- `i83.java:52-60`, `lrd0.java:3243-3245`, `d45.java:68-73`, `m3v.java:264-267`, `aa90.java:569+`: task ID `u` + four zero-padded eight-digit FNV-1a32 hashes of UTF-8 `part:index:lowercase_uuid`, parts 0 through 3.
- `a90.java:759-772`, `i83.java:286-310`: conversation-card Added state derives from listed task IDs. Unaccepted conversation-card proposals must stay out of the task listing.
- `clk.java:11-13`, `oo2.java:435-438`, `olt.java:62+`: conversation adoption sends empty title plus `{card_message_id,index}`, with agent as the default assignee.
- `o5k.java:63-64`, `wsi.java:13-21`, `bej.java:15-27`: native `found` and `cameFrom` provenance data. `wgj.java` accepts `foundBy:"chat"` for findings from ordinary chat.
- `iek.java:97-99`, `sij.java:511-545`, `nrx.java:626-643,1260-1294,1344+`: a suggested Found offer has native `found` information, is agent-created but assigned to the user, and has no execution/completion/retirement/taken state. In particular `iek.O` requires `found` and the plain pending-offer predicate; an agent-assigned task fails this predicate.
- `phk.java:7848+`: pending suggested-row dismissal uses `afk(wek)`; `amg.smali:5138`, `phk.smali:1992`, `bxf.java:91-96`: actual native DELETE includes `suggestion_only=true`. If the row fails the suggestion predicate, the same high-level UI action can instead take ordinary deletion, so a button label alone does not prove suggestion-only behavior.
- `y4b0.java:9+`, `bxf.java:74-78`: native handoff updates the task with `assignee:"agent"` through the ordinary task update API.
- `cv30.java:5149`, `ah0.java:116`, `d30.java:160-210`, `fck.java:2853`, `kzk.java:109-111`, `wal.java:64653`: empty Up next's 帮我看看 sends the ordinary prompt 看看有哪些你可以着手处理的任务, not a dedicated task-discovery endpoint. Normal channel POST is in `iz70.java:1473,1536`; server handler `aibo.py:1264`.

Original card parser remains `work/direct/base/smali/com/anthropic/claude/aibo/engine/e.smali:S4`, lines 35818-36170: server notice/body kind, UUID, native origin, 1..50 nonempty-title items and original `mbr` construction.

## Explicit producer placements

`propose_task_suggestions` accepts `{tasks:[{title,why}],placement?:"chat"|"up_next"}`. Default chat remains the original conversation-card behavior. Bounds are eight items per call, four successful proposal sources per current visible user turn, title up to 512 characters and why up to 20,000. Both placements reject private/inactive sessions, hidden/non-user origins and unavailable channels. No private routine producer is enabled by this change.

For `chat`, the producer checkpoints a real original-schema notice plus native channel event. Pending proposal records stay durable in `channel-tasks.json` but off ordinary task listings, preserving the native Add action. Native create/adopt validates the actual visible unchanged server notice and resolves title/why from its committed payload, never from client overrides.

For `up_next`, the producer creates actual listed Found offers, with `assignee:"user"`, `createdBy:"agent"`, `done:false`, `found:{why,foundAt,foundBy:"chat",toldInMessageId,...}` and `cameFrom:{suggestedAt,foundBy:"chat",toldInMessageId,...}`. Origin/told-in references name the actual user message. There is no acceptedAt, takenAt, session, thread, completed state or generated task_suggestion card. This avoids falsely marking a conversation card Added. The private server audit on that real user message records tool-call ID, exact proposal payload and original body/root revision; it is checkpointed before task references and never exposed as a native message or claimed external finding. Changed/deleted/private/foreign source data makes pending offers unavailable. Accepted work remains real history even after its source disappears.

The model tool description tells the model to choose up_next when the user asks to find useful/available work, and chat for optional conversational suggestions. There is no production substring router. The ordinary 帮我看看 prompt reaches the configured model and its tool decisions. Titles and reasons remain actual model output grounded in the visible request/channel context; the server does not invent external data, completed actions or approvals.

## Explicit acceptance and dismissal

Conversation adoption and listed Found handoff share one acceptance/dispatch helper and the existing task delegate/worker domain. Up next uses the original POST task update `{assignee:"agent"}`. Handoff validates committed source provenance, persists acceptedAt before dispatch and starts at most one real worker. Repeated/concurrent handoffs return that task, including after restart; they never replay execution automatically. Worker prompt uses the source title and why. After acceptance, normal task edits and lifecycle actions remain in the existing task owner.

Native DELETE with `suggestion_only=true` tombstones the association. Pending offers retire without running. Accepted tasks preserve sessions, execution, state and artifacts, including during a live run. Repeated dismissal is safe. Ordinary DELETE keeps its existing behavior. Pending offers cannot move before acceptance; moving accepted work clears the destination's old source association, while retaining real historical work in its original channel. Channel deletion uses existing cleanup. Native task writes recheck device authorization after acquiring the mutation lock, preventing a request queued before revocation from executing afterward.

## Persistence boundaries

Task-store changes stage a deep copy; failed writes restore the previous live idempotency view. A failed chat source checkpoint removes only that operation's dirty notice/event/pending notification. A failed Found source checkpoint removes/restores its private audit. Neither failure creates adoptable references. Successful source publication followed by failed task-reference commit leaves an inert durable source; explicit retries reuse it and commit references once. Changed placement or origin revision during that window is rejected.

Initial acceptance persists an explicit interrupted-before-start state. Failed initial acceptance rolls back so an explicit retry can commit cleanly. Once acceptance commits, an interrupted reservation is never automatically replayed. `_begin` changes phase before checkpointing and schedules its coroutine afterward, so recovery checks actual scheduled tasks and completed assistant evidence. A pre-dispatch checkpoint failure closes an unscheduled session as interrupted through the existing finish path and persists `interrupted_before_session_start`; a post-dispatch checkpoint failure preserves the actual running worker. Normal engine tool approvals remain in force.

## Validation and limits

`bridge.tests.test_task_suggestions` covers both placements, original IDs/schema/predicates, no pre-handoff worker, one real worker afterward, concurrent/fresh-ID retries, source revisions and cross-channel/private/deleted provenance, four-call budget, moving/deletion, retained accepted artifacts, restart and staged source/reference/acceptance/reservation/begin/dismiss failure windows. Real account/native-engine tests use a local mock model for the ordinary Chinese prompt, listed Found offers, native handoff/dismiss, persistence, and revocation while waiting on the task mutation lock. Existing `bridge.tests.test_channel_tasks` remains the task-domain regression suite.

Root's four-line native draft projection and three suggestion capability flags are retained. This implementation does not modify aibo/native/server/connector/admin code, original APK code, distribution files or external accounts. These tests use synthetic local data and a local mock provider; root separately validates the original native UI. No phone, live remote server, paid model or external credential was used for this subtask.
