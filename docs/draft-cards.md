# Native draft cards

This domain retains the Android message widget and POST action schemas. It does
not infer successful delivery from the client's `card_outcome.kind="sent"`.

## Retained source evidence

Paths below are relative to `work/direct-inspect-network/sources/defpackage/`.

- `ez30.java:1442,1659,2300,2357,2375–2567`: body discriminator
  `kind=widget,family=card,widget=message_compose_v1`; input fields `kind`,
  `variants`, `to`, `from_account`, `summary_title`; variants `label,subject,body`;
  kinds `email,textMessage,other`; native budgets 20 variants / 50,000 UTF-16
  characters; send projections `sends`, `server_saw_sent`, `agent_marked_sent`.
- Exact retained native gates additionally require top-level nonblank `text`
  (`smali_classes2/ez30.smali:5491–5505`, `smali/ln.smali:4659–4720`) and explicit
  `by_channel_session:false` for actual task-worker draft cards' task Send extraction
  (`smali/foq.smali:356–407`, `kjd0.java:1640`). Omitting text was observed to
  hide both the chat widget and specialized task draft in isolated native QA.
- `smali/ln.smali:2490–2544` additionally retains the main chat typed draft
  source only when the card is its own thread root. Actual APK scalar probes
  confirmed `rn.j=null` for the inherited user root and `rn.j=foq` when only
  the copied card's thread root was changed to its own ID. Main cards therefore
  keep `source_thread_root_id` privately and use their own native message root;
  task/discussion/trigger source messages retain the original task root.
  Those task-source cards also retain `in_timeline:false`, matching hidden task
  conversations, while their canonical history and `waiting_draft` remain
  available. Exact owned legacy cards with a true timeline flag repair once.
- Main coordinator cards use `by_channel_session:true`, derived only from
  canonical source-session equality with the actual channel's
  `channel_session_id`. `engine/e.java:7836–7882` applies `ln.e`, whose
  `java:124–130` policy validates coordinator cards against the real channel
  session set (`engine/e.java:1455`, `y83.java:358–360`). Worker cards use false;
  standalone placement alone never assigns coordinator role. No interactive
  classification or artificial bound session is invented to satisfy rendering.
- Generic compose widgets omit `server_card_kind`. `ez30.java:637,835` maps that
  wire field into `zy30.B`, and `o980.P` rejects a nonnull value on the ordinary
  typed widget render path. The JSON body still identifies `kind=widget` and
  the wire retains `server_notice:true`; dedicated suggestion/rule notice
  markers are separate contracts and are not altered.
- Tasks-list direct Send uses a separate pure `waiting_draft` clone with
  `message_compose_row_v1`, whose input is `{kind,variants:[{to,subject?,body,title?}]}`.
  Exact `kjd0.smali:6182–6229` requires one email variant, no explicit
  `from_account`, and no native attachments. We project this shape only for a
  real worker with an unsent single email variant and a recipient. Its native
  IDs, task decision, source root, revision and words remain the canonical
  card's; `summary_title` becomes the row's `title`. The original v1 history is
  unchanged, including its single variant label. All sent-state keys are
  omitted from the row clone because `kjd0` rejects their presence, even empty.
  Other kinds, multiple variants and explicit sender accounts keep the full v1
  preview with `waiting_draft_sendable:false`; no first variant is picked and
  no adapter is relabeled. Sender, ownership, source, channel and pending or
  unknown dispatch gates still control sendability. The Android client also
  applies its own text and address safety gates before exposing its action.
- `b770.java:32–79` has incomplete JADX loops. Exact edit fields are verified in
  `work/direct/base/smali_classes3/b770.smali:99–208`: `card_message_id`, optional
  `variant_index`, `subject`, `body`; the outcome includes kind, card ID, variant,
  edited flag and optional `card_edited_at`.
- `wrd0.java:530–600`: native edit compilation emits either reference-only
  edits or edited words. A reference is not authority to send.
- `jr3.java:145–154`, `lb6.java:66–79`: normal native send callbacks build `jpq`
  from original card ID, selected source variant, edited flag and source revision.
- Native edited Send uses `qf6.b`'s complete text envelope plus `edited:true`;
  `jr3/lb6` omit structured `card_draft_edits` in that path. The server decodes
  only the exact full grammar, binding its single-card/version prefix to the
  referenced selected variant. It accepts matching `draft-[0-9a-f]{8}` tags,
  an optional single-line Subject block and a mandatory My edit body block.
  `a410.smali:404–479` generates a tag absent from both edited fields; the
  server rejects collisions, mismatched or extra blocks and CR boundaries.
  Words are preserved exactly and pass the existing card and adapter checks.
  Ownership, task, reply and revision validation precede extraction. The
  original request and fingerprint remain unchanged, retaining immutable
  approved-payload retry binding. Ordinary text is never interpreted as edits;
  the structured-edit path retains its existing contract.
- `jbr.java:22–31`, `cx5.java:987–999`: task send additionally binds `task_id`,
  `task_card_id`, original `card_message_id`, `variant_index`, optional revision.
- `s570.java:17–30`: common native POST fields include timezone, reply anchors,
  optional quote, client message ID, flags, attachments and device signature.

## Integration

`AiboBridge.draft_cards` owns persisted records in the existing Aibo checkpoint.
It routes native action fields before text-only chat handling and cancels jobs
on shutdown. `DraftCards.install(engine)` registers flat engine tool definition
`create_message_draft` with handler `create(arguments, session_id)`:

After verified delivery, the sent flag, optional provider receipt and a plain
assistant confirmation commit together. The confirmation is a new terminal
`channel_message` in the action ACK's thread, timestamped after the ACK; card
updates alone do not finish Android's waiting reply state. Its durable ID is
stored on the attempt, and replay creates neither another send nor another
confirmation. Failed or unknown outcomes use accurate terminal notices instead.
Task notices retain hidden timeline visibility. Notices insert before an active
model placeholder and preserve the current phase, user/root IDs, session events
and activity flags, including when an older draft finishes in another thread.

```json
{"sender_id":"configured_owned_sender","input":{"kind":"email","to":"recipient","from_account":"own account","variants":[{"subject":"Subject","body":"Exact body"}]}}
```

Only registered server senders can produce actionable drafts. Model arguments
cannot select arbitrary tool names, credentials, raw tool arguments or accounts.
Installed production execution requires the exact active, visible model run
and owned initiating request (or real bound task). Private runs cannot publish.

Register a trusted adapter with:

```python
cards.register_sender(sender_id, send, current=current, accepts=accepts,
                      binding_id=stable_connector_tool_and_permission_identity)
```

- `accepts(input, source_session)` synchronously validates the visible recipient,
  sender identity and draft against immutable server base arguments.
- `current()` synchronously checks that the exact credential/tool/permission
  binding remains valid. The stable binding ID permits a same-identity restart;
  a changed credential epoch, schema or binding revokes old cards.
- Optional `cards.refresh_senders()` synchronously rebuilds registrations from
  already discovered local connector snapshots before create/action/projection
  lookups. It performs no network discovery and permits cold-start native sends
  without requiring an intervening model turn.
- `await send(payload, operation_id, recheck)` invokes the real configured tool.
  Payload contains only kind, to, from_account and selected variant words.
  Call `recheck()` immediately before irreversible dispatch, including after
  any asynchronous preparation or permission gate. Never/blocked policies still
  apply. Success requires `{"sent": True}` after actual verified delivery.
- Optional bounded success receipt fields are `tool,remote_id,url,operation_id`.
  Raw upstream results are discarded. `resolve_operation(operation_id)` returns
  a copied account/channel/session/card/task/sender binding for tool audit traces.
- `DraftSendFailed` certifies no external side effect; an explicit retry is safe.
  All other errors/cancellation/invalid success results are unknown outcomes and
  cannot dispatch that variant again. Native followup text distinguishes these.

Task integration sets `cards.task_binding = resolver`, a synchronous function
returning exactly `{"task_id": ..., "task_card_id": ...}` for a currently valid
owned source session, or `None` if cancelled/revoked/unbound. This binding is
checked on creation, action, and immediately before adapter dispatch.
The specialized Tasks-list binding is `AiboTaskRecord.card.id`, not the parent
Working notice or `data.threadCard.cardId`: `qp20.java:1031–1082` maps the native
decision card ID into `qij.a`, `tjd0.java:760–793` maps it into `akl.a`, and
`cx5.java:990` maps that into `jbr.task_card_id`. Ordinary task detail drafts may
use the normal card action without a specialized task-list binding. Set
`cards.source_current(session)` separately to validate actual task lifecycle
for every card (including ordinary task drafts) on creation and dispatch.

For the actual specialized projection, set `cards.task_identity(session)` to
the real task ID (independent of notices), and set
`cards.task_binding = cards.resolve_task_binding`. Draft creation then persists
its own stable task decision UUID. `cards.project_task(task_row, session)`
returns a copied task row with `card={id,state:"approve",options:[],message_id,
worded_at}`, `waiting_draft=<full ChannelMessageWire>`, and truthful
`waiting_draft_sendable`. Parent task fields and notices remain separate.
Pending/unknown send attempts make the waiting card unsendable; all-sent cards
leave the waiting projection. `ctd0.smali:5312–5333` confirms the native approve
state. New draft IDs follow `f2c.java:8` / `xh6.java:9`'s encoded
`cmsg_[0-9]{2}[base58]{44}` shape, required by `kjd0.java:1640` for task Send.

Implemented capability flags are `card_draft_edits`, `card_draft_edit_words`,
`card_draft_edits_reply`, `card_draft_edit_words_reply_v2`. Keep
`draft_send_press_waits` disabled: `vgd.java:107–117` explains its promise to
hold a Send until the agent's next pause. Keep `draft_card_send_call` disabled:
`wal.java:64985–65030` merges paired outgoing/incoming calls into a separate
native representation that this domain does not project. Dismiss, sent-message
cards and stale edit re-telling capabilities are not implemented here.

## Native behavior and persistence

A send action validates ownership, variant, task identity, source revision,
adapter identity and edits before reserving an operation. It persists an actual
authored user action receipt and normal `channel_message`, returns that new
message ID immediately, and executes the adapter in a background job. This
acceptance is never presented as delivery success. Successful actual execution
updates only the selected index in original `server_saw_sent` through
`channel_message_updated`. Other unsent variants remain available. The request
fingerprint and per-variant attempt survive restart; repeated requests reuse
the same authored receipt, and interrupted operations become unknown.
Each durable reservation stores the exact approved selected payload, canonical
whole-card input hash, accepted post-edit revision, original source revision,
sender identity and task binding. A certified no-effect retry must match all
current approval bindings and dispatches the stored words; it never reapplies
old edits to a subsequently changed card. Legacy failed attempts without these
bindings fail closed. A new explicit Send may approve the current revision.
Identical accepted historical requests validate authorization, account/channel
ownership and original task/card/variant references before returning their
receipt; completed task decisions need not remain waiting to read that receipt.
No send adapter is invoked by this historical lookup.
Live draft jobs also occupy the engine's normal active-task registry under
`draft_send:<operation_id>`, so existing health, maintenance and shutdown paths
observe the same task object. Maintenance rejects new dispatches with HTTP 409
before reservation; exact historical receipts remain readable. Registry cleanup
also handles cancellation before the coroutine first starts.
Channel deletion calls nonmutating `ensure_channel_idle(channel_id)` before
removing any other store, then `delete_channel(channel_id)` during the final
synchronous cleanup. Active deliveries block deletion; closed owned draft
records are purged with checkpoint rollback on failure, preserving other channels.
Producer/reservation checkpoint failures roll back uncommitted domain rows,
authored receipts, events, dedup entries and pending notifications. A real send
whose success receipt cannot commit retains an unknown non-replayable attempt
and does not expose a successful native card marker. Persistence errors are
propagated and logged; no delivery is invented to conceal a storage failure.

Edit references without `card_outcome` are ordinary explicit commentary about
drafts. Owned edited words persist; exact edit objects plus frozen source
revision and variant are included as quoted model context and private request
provenance, never silently discarded. Reference-only edits do not fabricate
unreceived edited words. Reply anchors and quotes retain ordinary thread
semantics. These requests never call a send adapter or authorize delivery.

Bodies preserve leading whitespace and trailing newlines. Unsupported
attachments/device signatures fail explicitly. Unknown fields, stale sends,
cross-account/channel/task references and edits to dispatched variants fail
without issuing a tool call. No Anthropic service fallback or replacement UI.
The generated widget text uses the owned summary title, first subject, or a
plain draft descriptor. Constructor migration repairs only the exact prior
canonical envelope of a persisted owned draft whose account/channel, source
author, card kind, visibility, timestamps and originating root are verified.
It preserves input words, IDs and created/edited revisions and publishes one
durable `channel_message_updated`; mismatched or unrelated history is untouched.
Restart is idempotent, and a failed checkpoint rolls back the repair without
notifying committed observers.
The same narrow verified repair recognizes the exact prior text-bearing
canonical envelope and corrects main-card placement while preserving original
source-thread provenance. It never globally changes task message roots or
selects one alternative from a multi-version card.
The exact prior false-role main envelope is also repaired to true only when its
verified author is the actual channel coordinator. Task-worker false values,
draft words, sent indices, IDs and created/edited revisions remain intact, and
the same checkpoint rollback and idempotence rules apply.
Migration accepts only absent or the exact old `widget` marker, requires all
the same owned-source and full canonical-body checks, and removes the obsolete
marker even when body and root already match. Unknown foreign markers are
neither repaired nor overwritten by ordinary draft publication.

Native QA follow-up established that the direct Tasks-list Send extractor
requires `message_compose_row_v1` with exactly one eligible flattened draft row;
`kjd0.java:1762–1772` returns no Send metadata for a v1 card. The v1 schema itself
correctly contains alternatives in `input.variants` (`rnq.java:448,562`). A
specialized one-row projection and actual task-detail native rendering remain
separate verification work; multiple alternatives must not silently become one
direct-send choice.

Validation is local with synthetic models/connectors only. Native source
compatibility is established from retained decoder/callback source; no new APK
build, device interaction, live external send, deployment or paid model run was
performed in this subtask.
