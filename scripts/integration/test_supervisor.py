import os
import tempfile
import unittest
from dataclasses import asdict

from supervisor import (
    ActiveEvent,
    EventStage,
    RuntimeState,
    SupervisorConfig,
    SwitchSupervisor,
)


class FakeAdapters:
    def __init__(self):
        self.pid = 4242
        self.polls = []
        self.current_account = "a@example.com"
        self.candidates = [
            {"account": "b@example.com", "account_ref": SwitchSupervisor._ref_for("b@example.com"), "eligible": True},
            {"account": "c@example.com", "account_ref": SwitchSupervisor._ref_for("c@example.com"), "eligible": True},
        ]
        self.preflight_ready = True
        self.preflight_status = "TRANSITION_PREFLIGHT_READY"
        self.adoption_verifier_available = True
        self.hot_adoption_verified = True
        self.restart_verified = False
        self.restart_status = "DESKTOP_RESTART_ADOPTION_UNVERIFIED"
        self.reconcile_restart_verified = False
        self.reconcile_restart_status = "DESKTOP_RESTART_RECONCILIATION_FAILED"
        self.credential_adoption_verified = True
        self.switch_safe_fail_refs = set()
        self.switch_uncertain_refs = set()
        self.resume_statuses = []
        self.probe_verified = False

        self.switch_calls = []
        self.prepare_calls = []
        self.credential_verify_calls = []
        self.hot_probe_calls = []
        self.restart_calls = []
        self.reconcile_restart_calls = []
        self.resume_event_ids = []
        self.probe_event_ids = []
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
        return {
            "verified": True,
            "account": self.current_account,
            "account_ref": SwitchSupervisor._ref_for(self.current_account),
        }

    def discover_candidates(self, session_id, current_account):
        return [x for x in self.candidates if x["account"].lower() != current_account.lower()]

    def prepare_account_transition(self, event_id):
        self.prepare_calls.append(event_id)
        return {"ready": self.preflight_ready, "status": self.preflight_status}

    def switch_account(self, account):
        self.switch_calls.append(account)
        ref = SwitchSupervisor._ref_for(account)
        if ref in self.switch_safe_fail_refs:
            return {"verified": False, "safe_to_retry": True, "error_code": "SYNTHETIC_SAFE_PREEXEC_FAILURE"}
        if ref in self.switch_uncertain_refs:
            return {"verified": False, "safe_to_retry": False, "status": "SYNTHETIC_UNCERTAIN_SWITCH"}
        self.current_account = account
        return {"verified": True, "safe_to_retry": False, "account_ref": ref}

    def verify_credential_adoption(self, expected_account_ref):
        self.credential_verify_calls.append(expected_account_ref)
        return {
            "verified": self.credential_adoption_verified,
            "status": "CREDENTIAL_TARGET_VERIFIED" if self.credential_adoption_verified else "CREDENTIAL_TARGET_UNKNOWN",
        }

    def desktop_adoption_verifier_available(self):
        return self.adoption_verifier_available

    def probe_desktop_adoption(self, expected_account_ref):
        self.hot_probe_calls.append(expected_account_ref)
        return {
            "verified": self.hot_adoption_verified,
            "status": "DESKTOP_ADOPTION_VERIFIED" if self.hot_adoption_verified else "DESKTOP_IDENTITY_MISMATCH",
            "language_server_pid": self.pid,
        }

    def restart_desktop_for_adoption(self, expected_account_ref, old_ls_pid):
        self.restart_calls.append((expected_account_ref, old_ls_pid))
        if self.restart_verified:
            self.pid = old_ls_pid + 100
            return {
                "verified": True,
                "status": "DESKTOP_ADOPTION_VERIFIED_AFTER_RESTART",
                "language_server_pid": self.pid,
            }
        return {"verified": False, "status": self.restart_status}

    def reconcile_desktop_after_restart(self, expected_account_ref, old_ls_pid):
        self.reconcile_restart_calls.append((expected_account_ref, old_ls_pid))
        if self.reconcile_restart_verified:
            self.pid = old_ls_pid + 100
            return {
                "verified": True,
                "status": "DESKTOP_ADOPTION_VERIFIED_AFTER_RESTART",
                "language_server_pid": self.pid,
            }
        return {"verified": False, "status": self.reconcile_restart_status}

    def resume_conversation(self, event_id):
        self.resume_event_ids.append(event_id)
        status = self.resume_statuses.pop(0) if self.resume_statuses else "TURN_STARTED"
        return {"status": status}

    def probe_resume_progress(self, event_id):
        self.probe_event_ids.append(event_id)
        return {
            "verified": self.probe_verified,
            "status": "TURN_PROGRESS_VERIFIED" if self.probe_verified else "TURN_PROGRESS_NOT_YET_VERIFIED",
        }


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

    def _persist_active_stage(self, stage, candidate="b@example.com", old_pid=None):
        baseline = self.a.create_quota_baseline("session-test", self.a.pid)
        active = ActiveEvent(
            event_id="evt_crash",
            detected_at_epoch=1.0,
            stage=stage.value,
            source_cursor=100,
            current_account_ref=SwitchSupervisor._ref_for("a@example.com"),
            candidate_account_ref=SwitchSupervisor._ref_for(candidate),
            pre_restart_ls_pid=old_pid,
        )
        self.s.store.save(RuntimeState(
            supervisor_session_id="session-test",
            baseline=baseline,
            active_event=asdict(active),
            processed_event_ids=[],
        ))

    def test_full_recovery_happy_path_hot_adoption(self):
        self.a.polls = [quota_event()]
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.prepare_calls, ["evt_1"])
        self.assertEqual(self.a.switch_calls, ["b@example.com"])
        self.assertEqual(self.a.restart_calls, [])
        self.assertEqual(self.a.resume_event_ids, ["evt_1"])
        state = self.s.store.load()
        self.assertIsNone(state.active_event)
        self.assertEqual(state.processed_event_ids, ["evt_1"])

    def test_safe_preexecution_switch_failure_rotates_to_second_candidate(self):
        self.a.polls = [quota_event()]
        self.a.switch_safe_fail_refs.add(SwitchSupervisor._ref_for("b@example.com"))
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com", "c@example.com"])

    def test_uncertain_switch_never_rotates(self):
        self.a.polls = [quota_event()]
        self.a.switch_uncertain_refs.add(SwitchSupervisor._ref_for("b@example.com"))
        r = self.s.run_once()
        self.assertEqual(r["status"], "SYNTHETIC_UNCERTAIN_SWITCH")
        self.assertEqual(self.a.switch_calls, ["b@example.com"])
        self.assertEqual(self.a.resume_event_ids, [])
        self.assertEqual(self.s.store.load().active_event["stage"], EventStage.FAILED_SAFE.value)

    def test_preflight_failure_blocks_before_switch(self):
        self.a.polls = [quota_event()]
        self.a.preflight_ready = False
        self.a.preflight_status = "TRANSITION_PREFLIGHT_BLOCKED"
        r = self.s.run_once()
        self.assertEqual(r["status"], "TRANSITION_PREFLIGHT_BLOCKED")
        self.assertEqual(self.a.switch_calls, [])

    def test_missing_adoption_verifier_blocks_before_switch(self):
        self.a.polls = [quota_event()]
        self.a.adoption_verifier_available = False
        r = self.s.run_once()
        self.assertEqual(r["status"], "BLOCKED_DESKTOP_ADOPTION_VERIFIER_UNAVAILABLE")
        self.assertEqual(self.a.switch_calls, [])
        self.assertEqual(self.a.resume_event_ids, [])

    def test_hot_mismatch_restarts_once_then_resumes(self):
        self.a.polls = [quota_event()]
        self.a.hot_adoption_verified = False
        self.a.restart_verified = True
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(len(self.a.restart_calls), 1)
        self.assertEqual(self.a.pid, 4342)
        self.assertEqual(self.s.store.load().baseline["language_server_process_id"], 4342)

    def test_restart_failure_fails_closed_without_resume(self):
        self.a.polls = [quota_event()]
        self.a.hot_adoption_verified = False
        self.a.restart_verified = False
        self.a.restart_status = "DESKTOP_RESTART_FAILED"
        r = self.s.run_once()
        self.assertEqual(r["status"], "DESKTOP_RESTART_FAILED")
        self.assertEqual(len(self.a.restart_calls), 1)
        self.assertEqual(self.a.resume_event_ids, [])

    def test_crash_after_switch_reconciles_without_second_switch(self):
        self._persist_active_stage(EventStage.SWITCH_ATTEMPTED)
        self.a.credential_adoption_verified = True
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, [])
        self.assertEqual(len(self.a.credential_verify_calls), 1)
        self.assertEqual(self.a.resume_event_ids, ["evt_crash"])

    def test_crash_after_restart_barrier_reconciles_without_second_restart(self):
        self._persist_active_stage(EventStage.RESTARTING_DESKTOP, old_pid=4242)
        self.a.reconcile_restart_verified = True
        r = self.s.run_once()
        self.assertEqual(r["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.restart_calls, [])
        self.assertEqual(len(self.a.reconcile_restart_calls), 1)
        self.assertEqual(self.a.pid, 4342)

    def test_repeated_quota_cycles_rotate_a_to_b_then_b_to_c(self):
        self.a.polls = [quota_event("evt_1", 100), quota_event("evt_2", 200)]
        r1 = self.s.run_once()
        r2 = self.s.run_once()
        self.assertEqual(r1["status"], "RECOVERY_COMPLETE")
        self.assertEqual(r2["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com", "c@example.com"])
        self.assertEqual(self.a.resume_event_ids, ["evt_1", "evt_2"])
        self.assertEqual(self.s.store.load().processed_event_ids, ["evt_1", "evt_2"])

    def test_quota_recurs_on_new_account_then_next_event_can_rotate_again(self):
        self.a.polls = [quota_event("evt_1", 100), quota_event("evt_2", 200)]
        self.a.resume_statuses = ["QUOTA_ERROR_OBSERVED", "TURN_STARTED"]
        r1 = self.s.run_once()
        r2 = self.s.run_once()
        self.assertEqual(r1["status"], "RECOVERY_QUOTA_RECURRED")
        self.assertEqual(r2["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com", "c@example.com"])

    def test_dispatch_unconfirmed_never_resends_on_next_run(self):
        self.a.polls = [quota_event()]
        self.a.resume_statuses = ["DISPATCHED_UNCONFIRMED"]
        r1 = self.s.run_once()
        self.assertEqual(r1["status"], "DISPATCHED_UNCONFIRMED")
        self.assertEqual(self.a.resume_event_ids, ["evt_1"])
        r2 = self.s.run_once()
        self.assertEqual(r2["status"], "MANUAL_RECONCILIATION_REQUIRED_AFTER_UNCONFIRMED_DISPATCH")
        self.assertEqual(self.a.resume_event_ids, ["evt_1"])

    def test_message_observed_waits_then_completes_without_resend(self):
        self.a.polls = [quota_event()]
        self.a.resume_statuses = ["USER_MESSAGE_OBSERVED_ASSISTANT_PENDING"]
        r1 = self.s.run_once()
        self.assertEqual(r1["status"], "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING")
        self.a.probe_verified = True
        r2 = self.s.run_once()
        self.assertEqual(r2["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.resume_event_ids, ["evt_1"])
        self.assertEqual(self.a.probe_event_ids, ["evt_1"])

    def test_baseline_invalid_rebaselines_without_switch(self):
        self.a.polls = [{"status": "BASELINE_INVALID", "cursor": 0}]
        r = self.s.run_once()
        self.assertEqual(r["status"], "REBASELINED_AFTER_INVALID_BASELINE")
        self.assertEqual(self.a.switch_calls, [])
        self.assertGreaterEqual(self.a.baseline_calls, 2)

    def test_no_new_event_commits_cursor(self):
        self.a.polls = [{"status": "NO_NEW_EVENT", "cursor": 77}]
        r = self.s.run_once()
        self.assertEqual(r["status"], "IDLE")
        self.assertEqual(self.s.store.load().baseline["committed_byte_offset"], 77)

    def test_processed_event_id_is_not_recovered_twice(self):
        self.a.polls = [quota_event("evt_1", 100), quota_event("evt_1", 150)]
        r1 = self.s.run_once()
        r2 = self.s.run_once()
        self.assertEqual(r1["status"], "RECOVERY_COMPLETE")
        self.assertEqual(r2["status"], "DUPLICATE_QUOTA_EVENT_IGNORED")
        self.assertEqual(self.a.switch_calls, ["b@example.com"])
        self.assertEqual(self.s.store.load().baseline["committed_byte_offset"], 150)


if __name__ == "__main__":
    unittest.main()
