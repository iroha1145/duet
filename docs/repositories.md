# Registered local repository tasks

This adapter supplies the retained native repository picker and task workflow from an explicitly registered local Git source. It does not claim a GitHub login, remote clone, fetch, push or pull-request integration. The APK includes RepositoryPickerBridge; server and APK must both support this adapter. Original global GitHub authentication is not changed.

## Owner configuration

Install Git and prepare a standalone local worktree owned/readable by the service user. Create `repositories.json` inside the private server data directory, while the service is idle/stopped, then restart to load it. There is no automatic source discovery or registration UI. Example:

```json
{
  "version": 1,
  "allowed_root": "/srv/duet-repositories",
  "repositories": [
    {"name": "local/project", "path": "project", "revision": "refs/heads/main"}
  ]
}
```

`allowed_root` must be an existing absolute directory. Each registered path is relative POSIX syntax beneath it, without traversal or linked components. Names use `owner/name` display syntax; they do not establish a remote account. Register an exact 40-character lowercase SHA-1 commit or explicit `refs/heads/...` local ref, not an arbitrary revision expression. A branch resolves at task preparation time; the resulting task retains its actual commit. At most 100 registrations are accepted. Protect this configuration like other private account data.

No configuration, missing Git or no available registration means no ready local repository execution. Authenticated availability and native listing use `/v1/code/aibo/repositories:availability` and `/organizations/{organization}/code/repos/all`, including `/api` aliases. Native labels retain their owner/name shape and use a distinct `duet-local://registered/...` source identity rather than falling back to GitHub URLs.

## Snapshot and execution

A start validates the current account/channel, explicit registration, request identity and approval context. Preparing a checkout uses only committed objects with bounded copying and Git execution; source config, credentials, hooks, filters, alternates and dirty worktree files are not imported. There is no network transport or source worktree mutation. Each accepted task has its own directory under `data/repository-tasks`, with detached HEAD and a sanitized local `.git`/index so ordinary local status/diff/commit work is possible. Changes stay in that task checkout.

Budgets are 256 MiB of objects/checkout data, 20,000 files/objects and 120 seconds of preparation. Symlinks, submodules, linked worktrees, unsafe paths and unsupported object/revision forms fail explicitly. These checks do not make arbitrary approved exec commands a multi-tenant sandbox.

File tools and exec resolve from the session's own repository root through the same tool approval mechanism. They do not silently use the global workspace. `publish_artifact` is refused for repository sessions until an explicit repository publication adapter exists; no private repository file is automatically registered as a global Make output. A model may still request approved commands, whose authority is that of the service user.

The native picker bridge captures the actual native owner/controller and server-session binding, checks readiness asynchronously, and rechecks identity/generation before opening the original picker. It does not claim a connection merely because a retained GitHub-shaped label is displayed. Session startup is keyed; stale, changed or unauthorized requests must not start an extra task. Persistence failure handling preserves the distinction between an uncommitted preparation and a durably committed task, including uncertain worker-start outcomes.

## Evidence boundary

Backend tests and independent fault probes cover committed-not-dirty reads, per-session tools, original approval, registration bounds, account/logout bindings and startup/persistence faults. APK build evidence must verify the actual signed DEX hooks and preservation of the original method bodies; source code alone does not establish that. Final device interaction, exact-bundle Linux acceptance and production deployment are recorded separately in the delivery verification document. This protocol describes behavior, not completion of those acceptance steps.
