import unittest
from desktop_identity import DesktopProcessSnapshot, DesktopRuntimeProbe, pseudonymize_email


class DesktopIdentityTests(unittest.TestCase):
    def snapshot(self, pid=55):
        return DesktopProcessSnapshot(10, r"C:\\Antigravity\\Antigravity.exe", pid, "secret", [1001, 1002])

    def test_identity_match_from_running_language_server(self):
        p = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (self.snapshot(), "OK"),
            user_status_fetcher=lambda snap: [{"userStatus": {"email": "b@example.com"}}],
        )
        r = p.probe_identity(pseudonymize_email("b@example.com"), 55)
        self.assertTrue(r["verified"])
        self.assertEqual(r["source"], "LANGUAGE_SERVER_GET_USER_STATUS")

    def test_identity_mismatch_fails_closed(self):
        p = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (self.snapshot(), "OK"),
            user_status_fetcher=lambda snap: [{"userStatus": {"email": "a@example.com"}}],
        )
        r = p.probe_identity(pseudonymize_email("b@example.com"), 55)
        self.assertFalse(r["verified"])
        self.assertEqual(r["status"], "DESKTOP_IDENTITY_MISMATCH")

    def test_ambiguous_identity_fails_closed(self):
        p = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (self.snapshot(), "OK"),
            user_status_fetcher=lambda snap: [
                {"userStatus": {"email": "a@example.com"}},
                {"userStatus": {"email": "b@example.com"}},
            ],
        )
        r = p.probe_identity(pseudonymize_email("b@example.com"), 55)
        self.assertFalse(r["verified"])
        self.assertEqual(r["status"], "DESKTOP_IDENTITY_AMBIGUOUS")

    def test_restart_waits_for_new_ls_pid_then_verifies(self):
        calls = {"inspect": 0, "restart": 0}
        old = self.snapshot(55)
        new = self.snapshot(77)
        def provider(hint):
            calls["inspect"] += 1
            if calls["inspect"] <= 1:
                return old, "OK"
            return new, "OK"
        def restart(snapshot, timeout):
            calls["restart"] += 1
            return True, "DESKTOP_RESTART_LAUNCHED"
        p = DesktopRuntimeProbe(
            process_snapshot_provider=provider,
            user_status_fetcher=lambda snap: [{"userStatus": {"email": "b@example.com"}}],
            restart_executor=restart,
            sleep_func=lambda _: None,
        )
        r = p.restart_and_verify(pseudonymize_email("b@example.com"), 55, ready_timeout_sec=1)
        self.assertTrue(r["verified"])
        self.assertEqual(r["language_server_pid"], 77)
        self.assertEqual(calls["restart"], 1)

    def test_restart_mismatch_stops(self):
        calls = {"inspect": 0}
        old = self.snapshot(55)
        new = self.snapshot(77)
        def provider(hint):
            calls["inspect"] += 1
            return (old, "OK") if calls["inspect"] == 1 else (new, "OK")
        p = DesktopRuntimeProbe(
            process_snapshot_provider=provider,
            user_status_fetcher=lambda snap: [{"userStatus": {"email": "wrong@example.com"}}],
            restart_executor=lambda snap, timeout: (True, "DESKTOP_RESTART_LAUNCHED"),
            sleep_func=lambda _: None,
        )
        r = p.restart_and_verify(pseudonymize_email("b@example.com"), 55, ready_timeout_sec=1)
        self.assertFalse(r["verified"])
        self.assertEqual(r["status"], "DESKTOP_IDENTITY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
