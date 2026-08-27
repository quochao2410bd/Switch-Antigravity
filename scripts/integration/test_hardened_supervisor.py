import os
import tempfile
import unittest

from hardened_supervisor import HardenedSwitchSupervisor
from supervisor import RuntimeState, SupervisorConfig
from test_supervisor import FakeAdapters, quota_event


class HardenedSupervisorTests(unittest.TestCase):
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

    def tearDown(self):
        self.tmp.cleanup()

    def test_processed_event_key_is_scoped_to_session_and_file_identity(self):
        self.a.polls = [quota_event("evt_1", 100)]
        sup = HardenedSwitchSupervisor(self.cfg, self.a, session_id="session-test")
        result = sup.run_once()
        self.assertEqual(result["status"], "RECOVERY_COMPLETE")
        processed = sup.store.load().processed_event_ids
        self.assertEqual(len(processed), 1)
        self.assertNotEqual(processed[0], "evt_1")
        self.assertTrue(processed[0].endswith(":evt_1"))
        self.assertIn("session-test:", processed[0])

    def test_same_raw_event_id_under_new_file_identity_is_not_global_duplicate(self):
        self.a.polls = [quota_event("evt_same", 100)]
        sup = HardenedSwitchSupervisor(self.cfg, self.a, session_id="session-test")
        first = sup.run_once()
        self.assertEqual(first["status"], "RECOVERY_COMPLETE")

        state = sup.store.load()
        state.baseline["file_identity"]["ctime_ns"] = 999999
        sup.store.save(state)
        self.a.polls = [quota_event("evt_same", 200)]
        second = sup.run_once()
        self.assertEqual(second["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.switch_calls, ["b@example.com", "c@example.com"])
        processed = sup.store.load().processed_event_ids
        self.assertEqual(len(processed), 2)
        self.assertNotEqual(processed[0], processed[1])

    def test_watchdog_process_restart_reuses_logical_session_and_cursor(self):
        self.a.polls = [{"status": "NO_NEW_EVENT", "cursor": 77}]
        first = HardenedSwitchSupervisor(self.cfg, self.a, session_id="session-persist")
        idle = first.run_once()
        self.assertEqual(idle["status"], "IDLE")
        self.assertEqual(self.a.baseline_calls, 1)
        self.assertEqual(first.store.load().baseline["committed_byte_offset"], 77)

        self.a.polls = [quota_event("evt_after_crash", 120)]
        restarted = HardenedSwitchSupervisor(self.cfg, self.a)
        self.assertEqual(restarted.session_id, "session-persist")
        recovered = restarted.run_once()
        self.assertEqual(recovered["status"], "RECOVERY_COMPLETE")
        self.assertEqual(self.a.baseline_calls, 2)  # one initial + one post-account-transition rebaseline
        self.assertIn("evt_after_crash", self.a.resume_event_ids)

    def test_legacy_raw_tombstone_remains_conservatively_duplicate(self):
        sup = HardenedSwitchSupervisor(self.cfg, self.a, session_id="session-test")
        baseline = self.a.create_quota_baseline("session-test", self.a.pid)
        sup.store.save(RuntimeState(
            supervisor_session_id="session-test",
            baseline=baseline,
            active_event=None,
            processed_event_ids=["evt_legacy"],
        ))
        self.a.polls = [quota_event("evt_legacy", 150)]
        result = sup.run_once()
        self.assertEqual(result["status"], "DUPLICATE_QUOTA_EVENT_IGNORED")
        self.assertEqual(self.a.switch_calls, [])
        self.assertEqual(sup.store.load().baseline["committed_byte_offset"], 150)


if __name__ == "__main__":
    unittest.main()
