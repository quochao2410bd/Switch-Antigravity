import os
import tempfile
import unittest

from supervisor import SupervisorConfig, SwitchSupervisor


class FakeAdapters:
    def __init__(self):
        self.pid = 4242
        self.polls = []
        self.current_account = "a@example.com"
        self.candidates = [
            {"account": "b@example.com", "account_ref": SwitchSupervisor._ref_for("b@example.com"), "eligible": True},
            {"account": "c@example.com", "account_ref": SwitchSupervisor._ref_for("c@example.com"), "eligible": True},
        ]
        self.switch_fail_refs = set()
        self.adoption_verified = True
        self.adoption_verifier_available = True
        self.resume_status = "TURN_STARTED"
        self.probe_verified = False
        self.switch_calls = []
        self.resume_calls = 0
        self.baseline_calls = 0

    def create_quota_baseline(self, session_id, ls_pid):
        self.baseline_calls += 1
        return {
            "status": "BASELINE_INITIALIZED",
            "committed_byte_offset": 10,
            "file_size": 10,
            "language_server_process_id": ls_pid,
            "supervisor_session_id": session_id,
            "file_identity": {"dev": 1, "ino": 2, "ctime_ns": 3, "size_at_creation": 10},
        }

    def poll_quota(self, baseline, session_id, ls_pid):
        if self.polls:
            return self.polls.pop(0)
        return {"status": "NO_NEW_EVENT", "cursor": baseline["committed_byte_offset"]}

    def current_ls_pid(self):
        return self.pid

    def get_current_account(self):
        return {"verified": True, "account": self.current_account, "account_ref": SwitchSupervisor._ref_for(self.current_account)}

    def discover_candidates(self, session_id, current_account):
        return list(self.candidates)

    def switch_account(self, account):
        self.switch_calls.append(account)
        ref = SwitchSupervisor._ref_for(account)
        if ref in self.switch_fail_refs:
            return {"verified": False, "error_code": "VERIFY_FAILED"}
        self.current_account = account
        return {"verified": True, "account_ref": ref}

    def desktop_adoption_verifier_available(self):
        return self.adoption_verifier_available

    def verify_desktop_adoption(self, expected_account_ref):
        return {"verified": self.adoption_verified, "status": "DESKTOP_ADOPTION_VERIFIED" if self.adoption_verified else "BLOCKED_DESKTOP_ADOPTION_UNVERIFIED"}

    def resume_conversation(self):
        self.resume_calls += 1
        return {"status": self.resume_status}

    def probe_resume_progress(self):
        return {"verified": self.probe_verified, "status": "TURN_PROGRESS_NOT_YET_VERIFIED"}


def quota_event(event_id="evt_1", cursor=100):
    return {
        "status": "NEW_CONFIRMED_QUOTA_EVENT",
        "cursor": cursor,
        "latest_event": {"event_id": event_id},
    }


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SupervisorConfig(
            log_path="synthetic.log",
            conversation_uuid="00000000-0000-4000-8000-000000000001",
            expected_agm_sha256="a" * 64,
            language_server_pid=4242,
            state_path=os.path.join(self.tmp.name, "state.json"),
            t03_journal_path=os.path.join(self.tmp.name, "journal.json"),
            resume_prompt="Continue exactly where you stopped.",
            max_rotation_attempts=3,
        )
        self.a = FakeAdapters()
        self.s = SwitchSupervisor(self.cfg, self.a, session_id="session-test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_recovery_happy_path(self):
        self.a.polls = [quota_event()]
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com"])
        self.assertEqual(self.a.resume_calls, 1)
        state = self.s.store.load()
        self.assertIsNone(state.active_event)
        self.assertEqual(state.processed_event_ids, ["evt_1"])
        self.assertEqual(state.baseline["language_server_process_id"], 4242)

    def test_switch_failure_rotates_to_second_candidate(self):
        self.a.polls = [quota_event()]
        self.a.switch_fail_refs.add(SwitchSupervisor._ref_for("b@example.com"))
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com", "c@example.com"])
        self.assertEqual(self.a.resume_calls, 1)

    def test_missing_adoption_verifier_blocks_before_switch(self):
        self.a.polls = [quota_event()]
        self.a.adoption_verifier_available = False
        r = self.s.run_once()
        self.assertEqual(r["status"], "BLOCKED_DESKTOP_ADOPTION_VERIFIER_UNAVAILABLE")
        self.assertEqual(self.a.switch_calls, [])
        self.assertEqual(self.a.resume_calls, 0)

    def test_desktop_adoption_unknown_blocks_before_resume(self):
        self.a.polls = [quota_event()]
        self.a.adoption_verified = False
        r = self.s.run_once()
        self.assertEqual(r["status"], "BLOCKED_DESKTOP_ADOPTION_UNVERIFIED")
        self.assertEqual(self.a.resume_calls, 0)
        state = self.s.store.load()
        self.assertEqual(state.active_event["stage"], "VERIFYING_DESKTOP")

    def test_baseline_invalid_rebaselines_without_switch(self):
        self.a.polls = [{"status": "BASELINE_INVALID", "cursor": 0}]
        r = self.s.run_once()
        self.assertEqual(r["status"], "REBASELINED_AFTER_INVALID_BASELINE")
        self.assertEqual(self.a.switch_calls, [])
        self.assertGreaterEqual(self.a.baseline_calls, 2)

    def test_dispatch_unconfirmed_never_resends_on_next_run(self):
        self.a.polls = [quota_event()]
        self.a.resume_status = "DISPATCHED_UNCONFIRMED"
        r1 = self.s.run_once()
        self.assertEqual(r1["status"], "DISPATCHED_UNCONFIRMED")
        self.assertEqual(self.a.resume_calls, 1)
        r2 = self.s.run_once()
        self.assertEqual(r2["status"], "MANUAL_RECONCILIATION_REQUIRED_AFTER_UNCONFIRMED_DISPATCH")
        self.assertEqual(self.a.resume_calls, 1)

    def test_no_new_event_commits_cursor(self):
        self.a.polls = [{"status": "NO_NEW_EVENT", "cursor": 77}]
        r = self.s.run_once()
        self.assertEqual(r["status"], "IDLE")
        self.assertEqual(self.s.store.load().baseline["committed_byte_offset"], 77)


if __name__ == "__main__":
    unittest.main()
