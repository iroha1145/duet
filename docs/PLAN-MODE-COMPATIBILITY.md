# Default/plan compatibility contract

These are the server authorization and compatibility boundaries. Device behavior requires separate verification.

## Behavior

- Models receive real `EnterPlanMode {}` and `ExitPlanMode {plan: string}` tool
  definitions. Exit accepts a complete nonempty UTF-8 plan up to 128 KiB.
- The actual engine session stores `permission_mode=default|plan` in SQLite.
  Initial SDK state and changes project `permissionMode` in system init/status
  events. SDK `set_permission_mode` supports only default and plan.
- Enter restricts subsequent calls, including remaining calls in the same model
  turn. Plan guards run before approval and again at dispatch. An approval that
  was pending when the user switches to plan cannot execute a write afterward.
  A local extension that suspends on a lock or metadata inspection also rechecks
  synchronously immediately before committing, using the current session mode
  and the existing rule/grant fingerprint. Revocation fails without asking for
  new approval while holding the local store lock.
- Plan permits native read_file/list_files; fixed registered handlers for
  list_triggers and list_message_draft_senders; and bound remote MCP handlers
  whose exact verified tool metadata declares boolean readOnlyHint=true without
  destructiveHint=true. All existing standing-rule and connector decisions still
  apply. Local queries are matched by handler identity, not their name. A remote
  annotation is the server's declaration, not independent proof of its behavior.
- Exit always waits for explicit approval, including under auto_approve. The
  native card uses display.kind=plan, a complete prose row and manual plan mode;
  it is not an ordinary tool approval card. Clients without can_plan are not
  served a plan card. Other plan modes are explicitly unsupported.
- SDK Exit allow accepts only updatedInput={} and the exact permission update
  [{type:setMode,mode:default,destination:session}]. Deny keeps plan and returns
  the APK's keep-planning instruction to the model. Native and SDK answers share
  the existing versioned single-decision reservation and receipts.
- Approving Exit only persists default. It never sets auto_approve, writes
  authorized_sessions, changes DontAsk, or grants exec/MCP permissions. Execution
  after approval requests its own ordinary approval. DontAsk remains limited to
  native read_file/list_files.
- Manual mode changes while Exit is pending return conflict; answer the plan
  first. Restart retains plan but interrupts pending work rather than replaying
  it; pre-restart tokens are stale.
- The legacy-token-only /sessions/{id}/approve route rejects allow for Exit and
  while in plan, so its `always` flag cannot create future execution grants.
  Deny can cancel the pending approval. Production account-login mode does not
  expose this legacy route. EngineBackend also rejects always grants for plan.

