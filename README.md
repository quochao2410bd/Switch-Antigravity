# Switch-Antigravity

A Windows-first watchdog and recovery layer for Antigravity Desktop.

## Goal

Keep long-running Antigravity coding tasks alive across quota exhaustion by:

1. Detecting confirmed quota exhaustion or terminal worker stop.
2. Switching to another pre-authorized Antigravity account through a pluggable account manager.
3. Verifying that the intended account is active.
4. Reopening or preserving the correct conversation.
5. Sending a deterministic resume prompt.
6. Continuing from the current repository state without repeating completed work.

## Design principles

- Desktop-first, Windows-first.
- No credential exfiltration and no secret material in Git.
- Prefer observable APIs/state over coordinate-based UI automation.
- Use layered recovery: direct desktop control first, supported CLI/thread import when available, Windows UI Automation only as fallback.
- Never rotate accounts indefinitely; stop cleanly when no eligible account remains.
- Git and local task state are the durable source of truth; conversation continuity is valuable but must not be the only recovery mechanism.
- Multi-agent development uses isolated branches and file ownership.

## Initial architecture

```text
Antigravity Desktop
        |
        v
  Watchdog Supervisor
   |      |       |
   |      |       +--> Conversation Resume Adapter
   |      +----------> Account / Quota Adapter
   +-----------------> Desktop State Detector
        |
        v
Persistent state + repository inspection
```

## Recovery state machine

```text
RUNNING
  -> QUOTA_SUSPECTED
  -> QUOTA_CONFIRMED
  -> CHECKPOINTING
  -> SELECTING_ACCOUNT
  -> SWITCHING_ACCOUNT
  -> VERIFYING_ACCOUNT
  -> RESTORING_CONVERSATION
  -> RESUMING
  -> RUNNING

Failure exits:
  -> BLOCKED_NO_ACCOUNT
  -> BLOCKED_CONVERSATION
  -> BLOCKED_USER_INPUT
  -> FAILED_SAFE
  -> COMPLETED
```

A quota signal must be confirmed through at least one strong signal or multiple weak signals. Inactivity alone must never trigger account rotation.

## Parallel research lanes

- T01: Antigravity Desktop process/log/state/conversation forensics.
- T02: quota/account switching, AGM integration, verification and security.
- T03: desktop conversation recovery and prompt submission automation.

The supervisor core is intentionally not owned by T01/T02/T03 during the research phase. It will be implemented after the three contracts are reviewed together.

## Status

Phase 0: repository bootstrap and parallel research.
