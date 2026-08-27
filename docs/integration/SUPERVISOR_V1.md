# Supervisor V1 Integration Contract

This branch reconciles the accepted T01/T02/T03 research lanes into a fail-closed Windows watchdog.

## Implemented

- T01 incremental quota detector with baseline bound to supervisor session + `language_server.exe` PID.
- Durable supervisor state using atomic `fsync + os.replace` persistence.
- Hardened dedupe key scoped to supervisor session + log file identity + event ID.
- Logical supervisor session/cursor recovery across watchdog process restart.
- T02 trusted AGM binary gate, fresh quota selection, bounded rotation and independently verified account transition.
- T03 UUID-only dry-run preflight before transition and again before any recovery send.
- Event-scoped T03 journals/prompts so the same conversation can recover from later quota events again.
- Durable crash barriers before account mutation and Desktop restart; a restarted watchdog reconciles rather than blindly repeating mutations.
- No blind resend after `DISPATCHED_UNCONFIRMED`.
- Repeated recovery cycles such as A -> B -> C.

## Desktop adoption verifier

`DESKTOP_ADOPTION_VERIFIER = IMPLEMENTED_PENDING_LIVE_VALIDATION`.

The integration layer verifies the account used by the running Antigravity Desktop through the exact `language_server.exe` local Connect-RPC endpoint:

`/exa.language_server_pb.LanguageServerService/GetUserStatus`

A positive result requires one qualified language-server process and one account identity returned by that running process. The identity is compared as a pseudonymous account reference. Ambiguous or missing evidence fails closed.

If hot adoption is not verified, Supervisor V1 permits at most one controlled Desktop restart for the recovery event, waits for a new language-server PID, verifies the running identity again, rebaselines T01, then reruns T03 preflight before any send.

## T03 crash reconciliation

Before a new send, the production adapter performs a read-only T03 preflight. Existing decisions are mapped without re-dispatch:

- `TURN_ALREADY_ACTIVE` -> reconcile as turn started.
- `RESUME_ALREADY_OBSERVED` -> wait/probe progress.
- `PREVIOUS_SUBMISSION_UNCONFIRMED` -> do not resend; manual reconciliation required.
- `NEW_ATTEMPT_ALLOWED` -> only decision that may enter the send path.
- Any unknown/corrupt/draft/UUID failure -> fail closed.

## CI

`.github/workflows/integration-ci.yml` runs on Ubuntu and Windows. Latest clean integration head `d58d95ed896660b60cc0970cb47bfd8da736a359` passed:

- Python compile checks.
- T01 Windows baseline/event contract.
- T02 supervisor trust contract.
- T03 recovery/resume suite.
- Supervisor, Desktop identity, crash reconciliation, repeated quota-cycle and hardened dedupe/session tests.

Green CI is necessary but is not proof of live Desktop behavior.

## Remaining live gates

PR #8 stays draft until all of these are validated on the user's actual Windows machine:

1. Read-only `GetUserStatus` identity probe on the currently running Desktop.
2. Read-only pinning of the installed AGM canonical path + SHA-256.
3. Read-only T03 dry-run proving the exact current conversation UUID is accessible and no user draft is present.
4. One controlled account transition without sending a recovery prompt; verify the new account in the running Desktop, using the single restart fallback only if needed.
5. Verify the original conversation UUID remains accessible after that transition.
6. Only then consider one deliberate real recovery-send validation.

## Retained boundaries

- `REAL SEND = NOT_LIVE_TESTED`.
- Cross-account visibility of an existing conversation UUID is runtime-dependent until live validation.
- `AUTONOMOUS_STALE_LOCK_RECOVERY = NOT_IMPLEMENTED`; ambiguous lock ownership remains fail-closed.
- No mathematical exactly-once claim.
