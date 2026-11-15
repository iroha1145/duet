# Owner-project Android push

Push is disabled until the owner configures their Firebase project, the user turns
on the native **Get notified** switch, Android grants notification permission,
and the current installation proves receipt of a registration challenge. An HTTP
registration response or Google's acceptance of a message does not establish
device receipt or visible OS delivery.

## Owner configuration

Use a Firebase Android application whose package name is `com.duet`.
The Android options and service-account credentials must belong to the same
project. Enable the Firebase Cloud Messaging HTTP v1 API and grant that service
account permission to send messages in that project. Project creation, billing,
credential provisioning, and a live device-delivery test are owner operations;
the local mock tests do not establish them.

With a current owner access token, `PUT /duet/notifications/fcm/config` accepts:

```json
{
  "enabled": true,
  "android": {
    "packageName": "com.duet",
    "projectId": "YOUR_PROJECT_ID",
    "projectNumber": "YOUR_PROJECT_NUMBER",
    "appId": "YOUR_FIREBASE_ANDROID_APP_ID",
    "apiKey": "YOUR_FIREBASE_ANDROID_API_KEY"
  },
  "service_account": {
    "type": "service_account",
    "project_id": "YOUR_PROJECT_ID",
    "private_key_id": "YOUR_KEY_ID",
    "private_key": "YOUR_PEM_PRIVATE_KEY",
    "client_email": "YOUR_SERVICE_ACCOUNT_EMAIL",
    "token_uri": "https://oauth2.googleapis.com/token"
  }
}
```

The unmodified service-account JSON may include its standard optional Google
metadata fields. Values above are placeholders, not usable credentials. Send
the actual configuration only over the authenticated private HTTPS endpoint.
Do not place the service-account JSON in the APK, source tree, screenshots, or
client preferences. The server stores it privately under `fcm/config.json` in
the bridge data directory; public GET responses omit it. Install the pinned
`google-auth[cryptography]` dependency from the server requirements.

`GET /duet/notifications/fcm/config` returns only `enabled`, `configured`,
`generation`, and the public `android` options. `PUT {"enabled":false}` disables
delivery while retaining configuration. A full PUT with `enabled:false`,
`android:null`, and `service_account:null` clears it. Every successful config
write rotates the generation and revokes existing push grants; clients must
register and prove receipt again. No client-supplied OAuth or FCM URL is used.

## Android registration and receipt

All endpoints below require the current owner bearer and the current device.
`GET /duet/notifications/fcm/client-config` adds `device_id` and `opted_in` to
the public configuration. `opted_in` is the native Get notified switch,
`sleepsort.enable_push`, and gates all pushes, including routine results.
The older `bogosort` category preference is not a second global switch.

Initialize the owner Firebase application only when configuration is enabled
and complete, the switch is on, and OS permission is granted. Keep a random
UUIDv4 installation ID stable for that install. Register the resulting token:

```http
POST /organizations/{organization_uuid}/notification/channels
Authorization: Bearer CURRENT_ACCESS_TOKEN
X-Duet-Installation-Id: INSTALLATION_UUID_V4
Content-Type: application/json
```

```json
{
  "channel_type": "FCM",
  "client_app_name": "com.duet",
  "registration_token": "DEVICE_FCM_TOKEN",
  "os_push_permission_granted": true,
  "form_factor": "phone",
  "timezone": "Asia/Tokyo"
}
```

Exactly these six body fields are accepted. `/api/organizations/...` is also
supported. The native nine-field channel response contains `channel_type`,
`client_app_name`, `client_platform`, `device_id`,
`last_successful_delivery_time`, `last_token_refresh_time`,
`registration_token`, `status`, and `uuid`. It contains no nonce. The channel
stays `INACTIVE` until a challenge delivered through FCM is acknowledged.
False or unknown OS permission cannot create a live grant. Tokens cannot be
bound to a different login device while still owned by the current one.

Every FCM data value is a string. Challenge data contains `duet_type` equal to
`registration_challenge`, `duet_event_id`, `duet_channel_uuid`,
`duet_generation`, `duet_config_generation`, `duet_device_id`,
`duet_installation_id`, `account_uuid`, `organization_uuid`, and `duet_nonce`.
The nonce is available only through this transport and expires after five
minutes. Verify the installation, login device, account, organization, and
current configuration before processing it. The challenge may arrive before
the registration HTTP response.

If encrypted local proof was reset or a prior offline channel DELETE could not
reach the server, registration may add `X-Duet-FCM-Prove: 1`. This rotates an
otherwise identical ACTIVE subscription to a new INACTIVE generation and
sends a fresh challenge. Repeated requests for the same still-pending,
unexpired challenge remain idempotent; they do not rotate it again. Use this
header only when local proof is absent. Missing the header preserves ordinary
refresh registration; values other than one exact `1` header are rejected.

```http
POST /duet/notifications/fcm/ack
Authorization: Bearer CURRENT_ACCESS_TOKEN
Content-Type: application/json
```

```json
{
  "channel_uuid": "duet_channel_uuid FROM FCM",
  "generation": "duet_generation FROM FCM",
  "event_id": "duet_event_id FROM FCM",
  "nonce": "duet_nonce FROM FCM"
}
```

The successful response has `outcome:device_received`, `status:ACTIVE`, and
`channel_uuid`. A stale generation is rejected; a challenge without its nonce
is rejected. Refresh an expired access token and retry the ACK with the new
token. Ordinary access-token refresh preserves a proven push subscription on
the same device: push is a separate grant bounded by that login's refresh
lifetime, not by its original access token's short lifetime. Logout, device
revocation, refresh-lifetime expiry, token or installation change, config
change, and turning Get notified off invalidate the applicable grants.

`DELETE /duet/notifications/fcm/channel` revokes only the authenticated device's
subscription and returns 204. This does not cancel any model or tool work.

## Normal notifications

Normal data uses `duet_type:notification` and the same identity and generation
fields, without a nonce. ACK its event with the same endpoint and three body
fields `channel_uuid`, `generation`, and `event_id`. Deduplicate event IDs
persistently on Android; retry after an ambiguous network outcome can deliver
the same event more than once. A normal notification must never establish a
new subscription generation or authorize an approval or tool action.

The data includes generic `title` and `body`,
`notification_feature_category` (`bogosort` or `routines`; explicit debug tests
may use `completion`), and
`ccr_notif_type` (`thread_waits_on_you`, `coordinator_reply`, or
`routine_completed`). Optional identifiers are `channel_id`, `session_id`,
`thread_root_id`, `task_id`, and `message_id`. `payload` is a JSON string with
service `duet`, method `OpenCodeSession`, and a request containing
`account_uuid`, `organization_uuid`, and `session_id`. Open the native session
through explicit application navigation after validating ownership; do not
execute arbitrary push URIs. Arguments, tool results, message text, private
routine drafts, and bearer tokens are excluded from notification data.

`POST /organizations/{organization_uuid}/notification/debug/test_push` accepts
`{"category":"completion"}` or `{"category":"bogosort"}`. It requires a
verified current channel and returns 202 for a queued event, not an OS-delivery
claim. `GET /duet/notifications/status` exposes only the caller's channel and
outbox readiness/counts plus sanitized errors. Badge delivery remains false.

## Committed sources, durability, and limits

Application setup binds the notification service once to the production
`AiboBridge`, `TaskCoordinator`, and routines service through `bind_sources`.
Only durably committed channel records are observed or replayed. Approval
state, ordinary turn completion, typed task results, and published
routine results are rechecked before send, so superseded approvals and private
routine drafts do not become notifications. Routine eligibility is also
checked against the current routine configuration.

Approval versions and task summaries are captured as server-only
`notification_facts` on the channel record before it is committed. This
metadata is excluded from the native channel wire. Replay derives the event
fact only from that immutable publication; current task or approval state may
suppress an outdated fact but cannot upgrade an older working card into an
uncommitted completion. Legacy records without the required facts are skipped
conservatively. New task results carry their fact on the real worker's existing
`thread_session_bound` publication, including tasks created directly, standalone
sessions, and scheduled work. A coordinator card is not required or fabricated.
New cards do not also carry task facts; historical committed card facts remain
readable for compatibility. Private routine work does not emit these bindings
or task notifications. Historical direct tasks without a fact are not turned
into new notifications after an upgrade.

Source cursors and relevant outbox events commit together in SQLite. Ignored
partial text frames advance an in-memory cursor and flush in a batch; there
is no per-token SQLite write. A checkpoint or notification-storage failure
does not abort model work. Committed replay retries notification ingestion.
Replay is bounded to 8,192 retained records / 8 MiB. If a cursor falls outside
retention, the service explicitly reconciles the gap: affected queued events
are cancelled and only retained, still-current records can be replayed. The
status records reconciliation; it does not promise recovery of every event
from an unavailable prefix.

The private SQLite outbox bounds devices (64), events (1,024), delivery rows
(4,096), source cursors (4,096), and compact ACK receipts (65,536). Under
capacity pressure, terminal delivered rows become exact event/device/
subscription/configuration receipt tombstones in the same transaction as
the new event and cursor. This preserves duplicate ACK success until expiry,
including across restart, without retaining full delivery rows. Nonce proof
is still required for challenge duplicates. Receipt bounds never evict
unexpired proof; saturated storage rejects new ingestion for retry. Full
source ingestion cannot prevent existing due deliveries from draining.
Expiry and bounded backoff apply, with a
maximum of eight send attempts. An interrupted send is retried with the same
event ID after restart. Every send checks current config, subscription,
permission, preference, login lifetime, and source state again after OAuth
awaits. A late Google invalid-token response revokes only the exact sent
subscription/token/configuration generation, so it cannot delete a newer
installation or token grant. Google responses are bounded and errors are sanitized. Provider
acceptance and Android receipt are distinct states; neither proves a visible
OS notification. Validate real delivery separately on the owner's configured
project and physical device.

## Local verification

`bridge.tests.test_fcm` exercises real authenticated HTTP routes, SQLite,
production source events, a synthetic RSA key, verified signed OAuth JWTs,
and local HTTP OAuth/FCM mock endpoints. It covers registration proof,
ownership, config/token/install changes, permission and preference revocation,
access refresh versus login expiry, retry/restart/expiry/bounds, source
checkpoint failure/replay/gaps, ordinary turns, task cards, and real routine
publication. `bridge.tests.test_aibo_committed` checks immutable committed
replay and real journal durability boundaries. These tests make no live
Google request and do not prove Firebase, Play services, OEM background
behavior, or Android notification visibility.
