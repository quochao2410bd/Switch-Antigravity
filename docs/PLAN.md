# Phase 0 Research Plan

## Objective

Establish the minimum verified contracts required to build a reliable watchdog for Antigravity Desktop on Windows.

The watchdog must recover from quota exhaustion without assuming that conversation continuity, account switching, or desktop automation behaves a certain way until that behavior is reproduced.

## Acceptance gates before supervisor implementation

### Gate A — quota/account contract

We must know:
- how quota exhaustion can be detected with a strong signal;
- whether AGM can enumerate/refresh/switch the intended desktop account on Windows;
- what exact post-switch verification proves the new account is active;
- whether app restart is required and what failure modes occur;
- how to stop safely when every eligible account is exhausted.

Owner: T02.

### Gate B — desktop/conversation contract

We must know:
- where Antigravity Desktop stores safe local conversation metadata/state;
- whether an existing conversation remains visible after account switch;
- how to identify the intended conversation deterministically;
- whether a stable conversation/thread identifier exists;
- what survives app restart and account switch.

Owner: T01.

### Gate C — resume automation contract

We must have at least one reproducible method to:
- attach to or control Antigravity Desktop;
- open/retain the intended conversation;
- focus the composer/input;
- submit a resume prompt;
- detect that submission actually started a new agent turn.

Preferred order:
1. stable desktop/CDP or exposed automation surface;
2. supported Antigravity CLI/thread import/resume bridge if appropriate;
3. Windows UI Automation;
4. coordinate-based mouse automation only as a last-resort experiment, not a production dependency.

Owner: T03.

## Recovery model to validate

```text
RUNNING
  |
  v
QUOTA_SUSPECTED
  | confirm
  v
QUOTA_CONFIRMED
  |
  v
CHECKPOINTING
  |
  v
SELECTING_ACCOUNT
  |
  v
SWITCHING_ACCOUNT
  |
  v
VERIFYING_ACCOUNT
  |
  +---- failure ----> BLOCKED_NO_ACCOUNT / FAILED_SAFE
  |
  v
RESTORING_CONVERSATION
  |
  +---- failure ----> FALLBACK_CONTEXT_REHYDRATION
  |
  v
RESUMING
  |
  v
RUNNING
```

## Signal policy

Never rotate solely because the process is idle or files have not changed.

Strong quota signals may include:
- explicit quota-exhausted UI/state/error;
- account manager reporting zero/insufficient quota for the active model;
- authoritative local log/status event indicating quota/resource exhaustion.

Weak signals may include:
- prolonged inactivity;
- process alive but no turn progress;
- unchanged repository state.

Weak signals can trigger investigation but not immediate account switching.

## Durable recovery principle

Conversation continuity is preferred but not sufficient as the only recovery mechanism.

Production supervisor should eventually retain a minimal local recovery record such as:
- project/repository path;
- active worker ID;
- conversation identifier/title if reliable;
- last active account identifier in non-secret form;
- last observed state;
- last successful recovery action;
- optional git HEAD/diff summary;
- retry counters and timestamps.

No secrets should be stored in this state file.

## Phase 1 after research

Once T01/T02/T03 results are reviewed:

1. Freeze adapter interfaces.
2. Implement state machine and persistent state store.
3. Implement quota detector + account switch adapter.
4. Implement conversation locator + resume adapter.
5. Add dry-run mode.
6. Add structured logs with secret redaction.
7. Add recovery backoff and maximum rotation count.
8. Add safe terminal states.
9. Build end-to-end tests using mocked quota/account/conversation adapters.
10. Run a controlled real-desktop acceptance test.

## Definition of done for MVP

A controlled test can demonstrate:

1. Start a long-running coding conversation in Antigravity Desktop.
2. Simulate or reach a confirmed quota stop.
3. Watchdog recognizes the quota condition without relying on inactivity alone.
4. Watchdog switches to another eligible, pre-authorized account.
5. Watchdog verifies the active account after switching.
6. Watchdog restores the same conversation when supported, or executes a documented safe fallback when not supported.
7. Watchdog submits the resume prompt exactly once.
8. The resumed worker inspects current repository state and continues without intentionally repeating completed work.
9. If no account is eligible, watchdog stops in `BLOCKED_NO_ACCOUNT` instead of looping.
10. No credentials or secret tokens appear in logs, Git history or test fixtures.
