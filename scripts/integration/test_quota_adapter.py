import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
T01 = os.path.abspath(os.path.join(HERE, "..", "t01"))
if T01 not in sys.path:
    sys.path.insert(0, T01)

from quota_detector import create_baseline, poll_new_events


QUOTA_LINE = (
    "ERROR: logging before google.Init: E0826 12:00:05.000000 99999 errorreport.go:223] "
    "agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
    "Please upgrade your subscription to increase your limits. Resets in 3h24m48s.\n"
).encode("utf-8")


class QuotaAdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "language_server.log")
        with open(self.path, "wb") as f:
            f.write(b"normal historical line\n")
        self.pid = 4242
        self.session = "session-test"

    def tearDown(self):
        self.tmp.cleanup()

    def baseline(self):
        baseline, code = create_baseline(self.path, self.pid, self.session)
        self.assertEqual(code, 0)
        self.assertEqual(baseline["status"], "BASELINE_INITIALIZED")
        self.assertEqual(baseline["language_server_process_id"], self.pid)
        self.assertEqual(baseline["supervisor_session_id"], self.session)
        return baseline

    def test_historical_quota_before_baseline_is_ignored(self):
        with open(self.path, "ab") as f:
            f.write(QUOTA_LINE)
        baseline = self.baseline()
        result, code = poll_new_events(baseline, self.path, self.pid, self.session)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "NO_NEW_EVENT")

    def test_new_exact_quota_event_is_detected(self):
        baseline = self.baseline()
        with open(self.path, "ab") as f:
            f.write(QUOTA_LINE)
        result, code = poll_new_events(baseline, self.path, self.pid, self.session)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "NEW_CONFIRMED_QUOTA_EVENT")
        self.assertEqual(result["latest_event"]["code"], 429)
        self.assertTrue(result["latest_event"]["event_id"].startswith("evt_"))

    def test_partial_record_does_not_advance_cursor(self):
        baseline = self.baseline()
        with open(self.path, "ab") as f:
            f.write(QUOTA_LINE[:-1])
        result, code = poll_new_events(baseline, self.path, self.pid, self.session)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "NO_NEW_EVENT")
        self.assertEqual(result["cursor"], baseline["committed_byte_offset"])
        self.assertGreater(result["trailing_partial_bytes_count"], 0)

    def test_replay_before_cursor_commit_has_same_event_id(self):
        baseline = self.baseline()
        with open(self.path, "ab") as f:
            f.write(QUOTA_LINE)
        first, first_code = poll_new_events(baseline, self.path, self.pid, self.session)
        second, second_code = poll_new_events(baseline, self.path, self.pid, self.session)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first["latest_event"]["event_id"], second["latest_event"]["event_id"])
        self.assertEqual(first["latest_event"]["event_record_sha256"], second["latest_event"]["event_record_sha256"])

    def test_session_mismatch_fails_closed(self):
        baseline = self.baseline()
        result, code = poll_new_events(baseline, self.path, self.pid, "wrong-session")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "BASELINE_INVALID")
        self.assertTrue(result["rebaseline_required"])

    def test_missing_baseline_is_rejected_before_poll(self):
        result, code = poll_new_events(None, self.path, self.pid, self.session)
        self.assertEqual(code, 5)
        self.assertEqual(result["status"], "BASELINE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
