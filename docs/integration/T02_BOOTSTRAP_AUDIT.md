# T02 AGM bootstrap audit correction

Coordinator audit against pinned upstream AGM revision `1d3ce8497e36ffa60c3b4e369168315a7ae4d469`.

## Correct storage model

At the pinned revision the default AGM data directory is `~/.antigravity-agent/` and the primary store is `cloud_accounts.db`, with `.mk` as the local master key. Do not describe this revision as using `accounts.json`.

## Import/login mutation boundaries

`agm import-ide` does not write Antigravity IDE `state.vscdb`, but it is not a purely read-only operation overall. It reads the current IDE session, performs token/userinfo/quota network work through AGM, writes/upserts the account in AGM's own database, marks the imported account active in AGM, and marks it active for the AGM IDE target.

`agm login` performs browser OAuth, stores encrypted account/token/quota material in AGM's own database, and marks the account active in AGM. It does not by itself switch the running Antigravity Desktop, Windows Credential Manager, or IDE database.

## Candidate-discovery mutation boundary

`ProductionAdapters.discover_candidates()` is not a read-only acceptance probe. For non-current accounts it calls `execute_safe_refresh(..., live_network=True)` before parsing the second `agm list`. A bootstrap read-only inventory check must therefore use trusted `agm list` parsing directly and can prove only that at least one distinct stored account exists. Fresh eligibility requires an explicitly mutating refresh-only phase against the AGM store.

## Binary provenance gate

The observed `%TEMP%\\agm.exe` hash proves identity of that file, not source equivalence to the pinned upstream revision. Before using AGM for account import/login/switch in production, prefer a controlled build from the pinned signed revision, hash the built binary, install it to a stable canonical path already supported by `trusted_agm_runner.py` (for example `%USERPROFILE%\\.local\\bin\\agm.exe`), and configure the supervisor against that exact path/hash.

## Current bootstrap verdict

`BOOTSTRAP_PLAN_NEEDS_CORRECTION`.

Do not run `agm import-ide`, `agm login`, or `agm switch` until the pinned-build/provenance step and the live T03 DOM fix are both reviewed.
