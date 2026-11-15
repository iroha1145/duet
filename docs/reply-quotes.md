# Native paragraph replies

The original Android renderer splits a single assistant Markdown message into
native AST rows. The backend preserves one assistant message ID and the exact
Markdown body. It does not split server messages on blank lines.

The original capability `reply_quote` enables native paragraph quote selection.
The message POST accepts `reply_to_message_id` (or the existing
`in_reply_to_message_id` alias) and nullable string `reply_to_quote`. Both parent
fields, if present, must agree. A non-null quote requires a parent, contains
non-whitespace text, and is limited to 500 Unicode code points. Supplementary
characters count once; isolated surrogates are rejected. This follows original
`cv30.y` and `j98.U`, which count Unicode code points up to 500 without adding an
ellipsis. Quote matching does not require a byte-identical substring: the native
client collapses whitespace and truncates at a grapheme boundary before sending.

For a non-null quote, the parent must be a completed or published timeline message in the same channel.
Unknown, foreign, hidden, and unpublished messages cannot be quoted. Current
login, channel ownership, archive, and disabled-state checks run after awaited
request/attachment processing. Existing client-ID deduplication and busy checks
remain in force before accepted message/provenance mutation.

An unquoted reply may also navigate a native task's hidden root when the actual
current task listing in that same channel exposes exactly its session and root
IDs. The target must be the session's completed root. This supports discussion
follow-ups, Make edits, and explicit follow-ups to genuine routine runs without
treating arbitrary hidden messages as reply targets. A running routine retains
the original busy rejection; only an accepted explicit follow-up can leave its
private-run state. Root navigation never grants permission to quote hidden
content, and old private messages retain their existing privacy markers.

Accepted user `body` and SDK/channel projections retain the actual user text.
Private persisted `_reply_to_message_id` and `_reply_to_quote` retain the actual
selected source. A separate private `_model_prompt` labels that excerpt as quoted
source data, not instructions, authority, or permission to execute actions, then
includes the current user request. The existing EngineService consumes that
prompt through its ordinary provider, Rules, and tool approval path. Both source
metadata and engine conversation context survive restart; duplicate retries do
not replace them or invoke the provider again. No paragraph IDs are invented.

The APK SendChannelMessageRequest serializer (`s570.java:14–18,54–64`) names
`reply_to_quote`; `reply_quote` is only the capability. The original
ChannelMessageWire serializer has no direct `reply_to_quote` field. The backend
therefore does not invent a response field to imply native historical quote
rendering. Native composer selection/POST and historical quote rendering still
need physical verification; this change establishes genuine quote ingress,
durable provenance, and model context, not unverified historical UI parity.

Targeted tests use actual HTTP routes, EngineService, SQLite, and a synthetic
loopback streaming provider. They cover paragraph B, raw body/SDK projections,
same-channel visibility, invalid/orphan/long/nonstring inputs, 500 emoji code
points, second-device deduplication, busy rejection, actual app restart, and
authorization/parent changes during an awaited body read. Existing attachment,
Aibo, native, and FCM tests cover unchanged integration boundaries.

Phone checklist: send the three-prose fixture; expect one assistant message ID
with three rounded bubbles. Copy B, then Reply B and inspect the native POST for
the original assistant ID plus normalized paragraph B in `reply_to_quote`.
Submit a follow-up and verify real stored/provider context. Reopen and compare
history without assuming historical quote chrome. Reactions remain
message-scoped; record actual read-aloud scope. Check fenced code with internal
blank lines, loose lists, tables, and single-paragraph soft line breaks. Fixtures
are under `testing/multi-bubble-native-fixtures.json`; only the owning root agent
operates the phone and remote provider.
