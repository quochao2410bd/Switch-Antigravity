# Supervisor V1 Integration Contract

This branch reconciles the accepted T01/T02/T03 research lanes into a fail-closed Windows watchdog state machine.

## What is implemented

- T01 incremental quota detector with bound supervisor session + language-server PID baseline.
- Durable supervisor state with atomic `fsync + os.replace` persistence.
- Quota-event ledger and cursor commit in the same durable state update, preventing blind replay while preserving incomplete event recovery.
- T02 trusted AGM binary gate, per-account refresh evidence, quota parsing, account selection, credential-store switch wrapper and independent credential identity verification.
- Bounded account rotation with pseudonymous runtime state; raw email addresses are kept in process memory only.
- T03 UUID-only conversation resume adapter and its durable duplicate-send recovery journal.
- Rebaseline gate after a verified account transition.
- No resend after `DISPATCHED_UNCONFIRMED`.
- Synthetic cross-lane supervisor tests.

## Current hard integration gate

`DESKTOP_ADOPTION_VERIFIER = NOT_IMPLEMENTED`.

T02 can prove that the Windows Credential Manager target changed to the expected Google account. Research did not prove that a running Antigravity Desktop / language server immediately adopts that new credential. Therefore production `ProductionAdapters.desktop_adoption_verifier_available()` currently returns `False` and Supervisor V1 stops **before performing an account switch**.

This is intentional. The integration layer must not mutate the credential store and then submit a resume prompt while Desktop account adoption is unknown.

A future verified adoption mechanism may be one of:

1. a read-only Desktop account identity signal exposed by Antigravity;
2. a controlled checkpoint + Desktop restart sequence followed by independent identity verification; or
3. another source-backed mechanism that proves the Desktop model request is now bound to the selected account.

Until then, end-to-end live rotation is `BLOCKED_DESKTOP_ADOPTION_VERIFIER_UNAVAILABLE`.

## Stale lock gate

T03 intentionally uses fail-closed file-lock contention and does not autonomously delete orphaned locks. `AUTONOMOUS_STALE_LOCK_RECOVERY = NOT_IMPLEMENTED`. An OS-owned lock backend or explicit safe reconciliation remains integration work.

## Live-test boundary

- Real account switching: not executed by the integration tests.
- Real T03 send: not live-tested.
- Synthetic supervisor orchestration: tested.
- No mathematical exactly-once claim.
