# Self-hosted check-ins and morning briefs

This implements real opt-in background work using the existing EngineService,
selected server model profile, enabled MCP connections, and current Rules. It
does not recreate Anthropic's private prompts or private scheduling service.
No official connectors, calendar entries, email, weather, or findings are
manufactured. The owner must configure an actual model before enabling a
routine; normal provider usage may incur the owner's provider charges.

## Native settings and identity

Register `register_routines(app, home, aibo, schedules, engine, config, tasks)`
after schedules, ChannelTasks, Workspace, Rules, Plugins and Notifications,
before the unknown-route fallback, in real account-login mode only. The
existing channel cleanup must continue calling `schedules.delete_channel`.
No new installer or APK patch is required. The server-owned persisted
`_private_run` boundary is shared by Aibo completion/recovery and the engine's
automatic workspace publication guard; ordinary chat behavior is preserved.

The existing native `POST /v1/code/aibo/project` accepts `check_ins_on`,
`morning_brief_on`, `routines_paused`, `time_zone`, and `quiet_hours`. Defaults
are OFF for both routines, UTC when no time zone was provided, and quiet hours
23:00–08:00. Two real disabled plans are materialized on first project ensure;
their stable IDs are returned in `routine_switches` with native states and
offers. ON requires the configured actual server model.

`quiet_hours.start/end` are whole hours 0–23 and the duration modulo 24 must be
1–21 hours. `time_zone` must be recognized by `zoneinfo`. Existing stores with
the earlier, incorrect minute-based quiet hours need an explicit corrected
setting before routines can run; they are not silently converted.

The project store owns consent; the existing trigger journal owns plans and
durable claims. Changes take the scheduler lock and contain no await while
committing settings/plans. Dispatch checks both records, preventing a trigger
write alone from authorizing work after an interrupted update. No distributed
multi-process locking is provided; run one scheduler process per data directory.

Settings are account/organization server state, shared by every logged-in
device. Plans bind to the current main channel and owner. Main-channel reset
permanently invalidates old identities and clears their ON preferences. Fresh
disabled IDs can then be created for the replacement main channel. Deleting a
plan does not silently restore it; an explicit switch request can create a new
ID. Builtin prompts/cadences are not editable through generic trigger edits.

## Implemented scheduling policy

Morning briefs run once per local calendar date, when quiet hours end. DST
gaps move a nonexistent wall time forward; folds use its first occurrence.
The last claimed morning date prevents a second same-date run after settings
changes. Stale prior-date mornings are skipped rather than replayed.

Check-ins default to hourly absolute-time slots outside local quiet hours.
This frequency is a self-hosted policy, **not a recovered official cadence**.
The authenticated owner endpoint `GET /duet/routines/policy` reports it;
`PUT` accepts exactly `{"check_in_minutes": 15|30|60|120|180}`. Future check-in
slots are recomputed when that policy changes. No credentials are accepted by
this endpoint. Native generic cron metadata is only an approximate description
for builtin check-ins; the policy endpoint and scheduler determine actual slots.

Global pause blocks new builtin and other recurring tasks. One-time reminders
still fire and already running work continues. Resume/time-zone/quiet-hour
changes compute future builtin slots, without a burst of missed check-ins.
Automatic delivery also rechecks the current time zone and quiet hours when
the model calls delivery, even for work started earlier. Findings suppressed
by quiet hours are not queued, replayed, or reported as successfully sent.
The run and any real user approval complete normally; explicit ordinary chat
and one-time reminders do not use this automatic delivery filter.
Archived or disabled channels are rejected before a claim or next-fire advance.
One routine never overlaps its own active run. Claims and intended session IDs
are saved before starting actual model work; interrupted claims are surfaced
and never automatically replayed after restart.

## Silent work, delivery, and permissions

A real engine run receives bounded current main-chat history and task context,
the normal channel context, current model profile, enabled MCP snapshot, and
standing rules. Quoted material is explicitly treated as data. Ordinary model
text stays in the engine session transcript: there are no channel text deltas,
partial replies, or empty routine reports. The Tasks row still exposes the
actual worker and its real state.
Private runs also suppress the normal `write_file` automatic Make publication:
an approved write still creates its actual workspace file, but does not attach
or snapshot a private draft as an output. Before the write executes, Workspace
persists private path provenance. Unpublished Make indexing, Files listing,
raw reads/downloads/views, and raw artifact IDs exclude that live path, even
after restart. An old explicitly published immutable snapshot remains readable
with its old public bytes when the live file is privately overwritten. Only
an actual successful ordinary write/HTTP save or ordinary `publish_artifact`
action promotes the current file; finishing a routine or opening a new turn
does not. A proven failed write which left the bytes unchanged restores the
previous visibility; partial/unknown changes stay sealed.
Aibo's common finish path uses this
same persisted boundary after restart, clears pending approvals, and preserves
the interrupted status without manufacturing a channel message.
A new explicit user turn can resume the execution session normally. Only an
accepted new turn clears its private-run marker after the archive, duplicate
and busy guards; retries and rejected requests cannot relax an active run.
The old private message roots remain hidden, including after restart, while
the newly requested reply and its authorized outputs use ordinary chat rules.

This is a tracked `write_file` provenance policy, **not complete filesystem
isolation**. An approved `exec` command, local MCP server, or another process
may write or copy workspace files outside this boundary. Such arbitrary writes
are not claimed to remain private and may be discovered by normal workspace
indexing. The routine catalog still exposes the native `exec` tool when the
selected model allows tools; its ordinary approval/auto-approval and Rules
remain unchanged. No broad filesystem observer or new sandbox is provided.

Only `publish_routine_result` can deliver a routine finding to the owning main
chat. Its definition/handler exists solely in the real worker's ContextVar
snapshot; ordinary chat cannot call it or choose a target channel/session.
The worker catalog excludes user-turn autoapproved child-task/progress/artifact
shortcuts so they cannot create an alternate visible output path.
Delivery rechecks the current main channel, owner, live plan identity, ON
consent, and real run/session binding. A result needs at least one source ID
from the supplied actual context or a successful tool call in that run.
Native tool errors, nonzero command exits, failed result status and MCP
`isError: true` cannot serve as successful sources.
This validates source existence, not semantic relevance or model accuracy.
The prompt directs check-ins to report only things that cannot wait and briefs
only relevant items for today; nothing qualifying means no delivery.

The already authorized internal delivery defaults to allowed, like task
progress. Current Rules `never` and `ask_first` still override it. Its name is
enumerated for native rule proposals without exposing the tool to ordinary
chat. All other tools retain their actual normal approval and connector policy;
no external-action permissions are granted by switching ON.

ASK decisions emit actual native requires-action events and appear as
`needs_input` in the real Tasks worker. They can be approved, declined, or
cancelled using the existing native flows. Switching the builtin OFF cancels
its pending-approval workers and prevents later delivery. Global pause leaves
already running jobs intact. Delivery is idempotent per persisted run and uses
native `routine_relay`, `author_routine_id`, and bound-session purpose
`ROUTINE_RUN`; native device rendering still requires device verification.

System push uses the separately configured owner Firebase project and a
device-verified registration. Successful main-chat delivery alone does not
mean a background OS notification was sent. Routine-specific model overrides
are disabled: native config returns `can_pick_routine_models: false` and null
overrides. Every run uses the currently selected real server model; model
selection and provider credentials stay on the server.
The recovered model endpoint is `GET/PATCH /v1/code/channels/{channel_id}/config`
with `config.{can_pick_routine_models,check_in_model,morning_brief_model}` and
PATCH fields `check_in_model`, `clear_check_in_model`, `morning_brief_model`,
`clear_morning_brief_model`. Implementing it requires a real catalog-profile
binding; there are no invented official model IDs here.

## Evidence and tests

The original Android request includes `find_only: true` when explicitly
pausing routines or changing either builtin switch. This prevents creating a
missing project; it does not discard an update to an existing project. A pure
lookup remains read-only, and an update rechecks authorization and the same
project after acquiring the schedule lock.

Recurring schedules carry `CRON_TZ=<IANA zone>` in the native response's
`cron_expression`. The original Android parser treats a bare expression as UTC;
omitting this prefix incorrectly displays an 08:00 Tokyo morning as 17:00.
Execution retains the stored five-field cron and its separate IANA zone.
Updates accept either native `CRON_TZ=` / `TZ=` syntax or a bare expression with
the existing `timezone` field, and reject conflicting explicit zones.

Native contracts: `q3s` switch keys, `fhd.e0` capability parsing, `zq8.L` state
and offer parsing, `c7v` project request, `hqd` hour validation, `cvq` relay,
`huq` message fields, `iuq` bound-session fields, and `dl2/p5r/l5r/n5r` model
configuration. Native string resources `0x7f100d3f–0x7f100d41` establish silent
findings, quiet-end mornings, recurring pause, and one-time reminder behavior.

`bridge/tests/test_routines.py` uses actual EngineService, tool approval,
native Rules confirmation, persistent stores, and a loopback HTTP provider
fixture. It also exercises unmodified `create_app` registration with real
account auth and Workspace hooks, a running-session crash snapshot/reopen,
and an actual loopback MCP transport failure response. No paid provider,
phone, APK build, VM, or installer is involved.
