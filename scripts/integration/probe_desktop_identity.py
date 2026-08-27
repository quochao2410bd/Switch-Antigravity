#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from desktop_identity import DesktopRuntimeProbe, pseudonymize_email


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only Antigravity Desktop language_server identity probe")
    p.add_argument("--language-server-pid", type=int)
    p.add_argument("--expected-email", help="Optional expected account; never printed raw")
    args = p.parse_args()

    probe = DesktopRuntimeProbe()
    expected_ref = pseudonymize_email(args.expected_email) if args.expected_email else "__no_expected__"
    if args.expected_email:
        result = probe.probe_identity(expected_ref, args.language_server_pid)
    else:
        snapshot, status = probe.inspect(args.language_server_pid)
        if snapshot is None:
            result = {"verified": False, "status": status, "detected_account_ref": None}
        else:
            try:
                responses = probe._user_status_fetcher(snapshot)
                refs = set()
                for response in responses:
                    user_status = response.get("userStatus", {}) if isinstance(response, dict) else {}
                    email = user_status.get("email") if isinstance(user_status, dict) else None
                    if isinstance(email, str) and "@" in email:
                        refs.add(pseudonymize_email(email))
                if len(refs) == 1:
                    result = {"verified": True, "status": "DESKTOP_IDENTITY_OBSERVED", "detected_account_ref": next(iter(refs)), "language_server_pid": snapshot.language_server_pid}
                elif len(refs) > 1:
                    result = {"verified": False, "status": "DESKTOP_IDENTITY_AMBIGUOUS", "detected_account_ref": None}
                else:
                    result = {"verified": False, "status": "DESKTOP_IDENTITY_EMAIL_MISSING", "detected_account_ref": None}
            except Exception:
                result = {"verified": False, "status": "DESKTOP_GET_USER_STATUS_FAILED", "detected_account_ref": None}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
