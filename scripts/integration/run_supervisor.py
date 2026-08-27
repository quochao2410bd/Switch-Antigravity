#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os

from supervisor import SupervisorConfig, SwitchSupervisor
from production_adapters import ProductionAdapterConfig, ProductionAdapters

DEFAULT_PROMPT = """Continue the current task from exactly where you stopped.
First inspect the current repository state, git status, git diff, recent commits, terminal output and the existing conversation context.
Do not redo completed work.
Continue implementing only the remaining work.
Run the required tests when implementation is complete.
If the original task is already complete, verify it instead of starting unrelated work."""


def main() -> None:
    p = argparse.ArgumentParser(description="Switch-Antigravity Supervisor V1")
    p.add_argument("--log-path", required=True)
    p.add_argument("--language-server-pid", required=True, type=int)
    p.add_argument("--conversation-uuid", required=True)
    p.add_argument("--expected-agm-sha256", required=True)
    p.add_argument("--expected-agm-path")
    p.add_argument("--state-path", default=os.path.expanduser(r"~\.switch_antigravity\supervisor_state.json"))
    p.add_argument("--t03-journal-path", default=os.path.expanduser(r"~\.switch_antigravity\t03_recovery_journal.json"))
    p.add_argument("--model-group", default="gemini-pro", choices=["gemini-pro", "gemini-flash", "claude"])
    p.add_argument("--min-quota-pct", type=int, default=20)
    p.add_argument("--max-rotation-attempts", type=int, default=3)
    p.add_argument("--poll-interval-sec", type=float, default=2.0)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--execute-switch", action="store_true", help="Allow verified AGM credential-store switch. Desktop adoption gate still fails closed until a verifier is integrated.")
    p.add_argument("--forever", action="store_true")
    args = p.parse_args()

    cfg = SupervisorConfig(
        log_path=args.log_path,
        conversation_uuid=args.conversation_uuid,
        expected_agm_sha256=args.expected_agm_sha256,
        language_server_pid=args.language_server_pid,
        state_path=args.state_path,
        t03_journal_path=args.t03_journal_path,
        resume_prompt=args.prompt,
        model_group=args.model_group,
        min_quota_pct=args.min_quota_pct,
        max_rotation_attempts=args.max_rotation_attempts,
        poll_interval_sec=args.poll_interval_sec,
    )
    adapters = ProductionAdapters(ProductionAdapterConfig(
        log_path=args.log_path,
        conversation_uuid=args.conversation_uuid,
        expected_agm_sha256=args.expected_agm_sha256,
        expected_agm_path=args.expected_agm_path,
        language_server_pid=args.language_server_pid,
        t03_journal_path=args.t03_journal_path,
        resume_prompt=args.prompt,
        model_group=args.model_group,
        min_quota_pct=args.min_quota_pct,
        max_rotation_attempts=args.max_rotation_attempts,
        execute_switch=args.execute_switch,
    ))
    sup = SwitchSupervisor(cfg, adapters)
    if args.forever:
        sup.run_forever()
    else:
        print(json.dumps(sup.run_once(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
