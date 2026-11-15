# Private helper channels

Helpers reuse the retained native `EnsureCodeProjectRequest.helper` contract and `aibo_helper` channel template. They are local single-owner channels, independent of Anthropic accounts, subscription state or hosted agents. Retaining a native entry does not prove that the original account exposes it: the Android UI keeps its original conditional availability rather than forcing an obsolete/retired entry open.

## Creation and identity

The existing authenticated project endpoint `/v1/code/aibo/project` (also `/api/v1/code/aibo/project`) accepts a `helper` object with only the supported `client_request_id`, `name`, `purpose` and `icon` fields; the request key and name are required, while purpose/icon may be omitted. Other helper fields and unrelated top-level project mutations are refused. The client request key is required; an exact replay resolves the same owned helper instead of creating a duplicate, while changed words under the same key conflict. Helper settings belong inside this object, not the main project's settings.

A helper receives a distinct private owned coordinator/channel with its own ID, name, purpose and icon. Creation does not replace the main project, reset main routines/preferences, or route messages through a shared hidden worker. Its purpose supplies helper-specific instructions using the normal model/tool snapshot and approval mechanism. The advertised `helper_keys` capability is present only with the local engine.

## Context and lifecycle

Private means an owned channel inside this single-account server, not a secret from the main Everything channel. Everything intentionally aggregates completed messages and topics from active helper/room channels. Another helper reads its own channel context, not the full Everything aggregate. In-flight partial output is not presented as completed cross-channel context. This is not a cross-user or multi-tenant confidentiality guarantee.

Helpers use the normal archive/disable/tombstone, persistence, restart and multi-device lifecycle. A deleted helper is not resurrected by replaying an old creation key; invalid/stale ownership or conflicting identity is refused. Model, MCP and file/exec access retain the server's usual tool approval and standing rules; helper creation does not approve tools or grant new external service access.

Local HTTP/mock-model checks and an independent private-channel probe cover distinct coordinator ownership, main-project preservation, request replay/conflict, purpose/cache stability and lifecycle/restart behavior. Native UI reachability and production acceptance are recorded separately in the final verification document. No real user credentials, screenshots or test databases belong in the protocol package.
