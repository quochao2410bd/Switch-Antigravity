# Agent Collaboration Rules

This repository is developed by a main orchestrator plus parallel agents T01, T02 and T03.

## Identity rule

Every task prompt sent to an agent must begin and end with that agent ID. Every report returned by the agent must also begin and end with the same agent ID.

## Branch ownership

- T01 branch: `research/T01-antigravity-desktop`
- T02 branch: `research/T02-account-quota`
- T03 branch: `research/T03-conversation-resume`

No agent may push to another agent's branch or directly to `main`.

## Phase 0 file ownership

T01 owns only:
- `docs/t01/`
- `scripts/t01/`
- `tests/fixtures/t01/`

T02 owns only:
- `docs/t02/`
- `scripts/t02/`
- `tests/fixtures/t02/`

T03 owns only:
- `docs/t03/`
- `scripts/t03/`
- `tests/fixtures/t03/`

Main orchestrator owns:
- `README.md`
- `AGENTS.md`
- `docs/PLAN.md`
- `watchdog/`
- `src/switch_antigravity/`
- shared CI/configuration files unless explicitly delegated later.

## No-overwrite rule

Agents must not edit shared files during Phase 0. If a shared change is needed, describe it in the final report instead of making the change.

## Git discipline

1. Fetch latest `main` before starting.
2. Record the base SHA in the report.
3. Commit coherent changes only on the assigned branch.
4. Never force-push over another agent's history.
5. Never merge your own PR.
6. Open one PR to `main` when the assigned research package is complete.
7. PR title must start with the agent ID.

## Security rules

Never commit or print passwords, access tokens, refresh tokens, OAuth secrets, MFA material, browser profiles, Windows Credential Manager secret contents, AGM master keys, decrypted account stores or raw authentication databases containing secrets.

Research may document safe metadata such as executable/process names, paths, redacted schemas, command names, exit codes and observable state transitions.

## Evidence standard

Clearly label each finding as one of:
- VERIFIED: reproduced locally or directly confirmed from authoritative source/code.
- LIKELY: strong evidence but not yet reproduced.
- UNKNOWN: not established.

Do not present guesses as implementation facts.

## Final report contract

The report must contain:
- agent ID at first line;
- base SHA;
- branch name;
- commits created;
- files changed;
- tests/reproductions performed;
- VERIFIED findings;
- LIKELY findings;
- UNKNOWN/blockers;
- security observations;
- recommended interface/contract for the main supervisor;
- PR number/link if opened;
- agent ID again as the final line.
