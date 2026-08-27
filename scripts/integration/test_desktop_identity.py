import unittest

from desktop_identity import DesktopProcessSnapshot, DesktopRuntimeProbe, pseudonymize_email


def snap(ls_pid=4242):
    return DesktopProcessSnapshot(
        main_pid=1111,
        main_executable=r"C:\Antigravity\Antigravity.exe",
        language_server_pid=ls_pid,
        csrf_token="synthetic_csrf",
        listening_ports=[50001],
    )


class DesktopIdentityTests(unittest.TestCase):
    def test_exact_language_server_email_match_verifies(self):
        probe = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (snap(), "OK"),
            user_status_fetcher=lambda s: [{"userStatus": {"email": "b@example.com"}}],
        )
        expected = pseudonymize_email("b@example.com")
        result = probe.probe_identity(expected, 4242)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_ADOPTION_VERIFIED")
        self.assertEqual(result["source"], "LANGUAGE_SERVER_GET_USER_STATUS")

    def test_mismatched_language_server_email_fails_closed(self):
        probe = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (snap(), "OK"),
            user_status_fetcher=lambda s: [{"userStatus": {"email": "a@example.com"}}],
        )
        result = probe.probe_identity(pseudonymize_email("b@example.com"), 4242)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_IDENTITY_MISMATCH")

    def test_ambiguous_user_status_emails_fail_closed(self):
        probe = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (snap(), "OK"),
            user_status_fetcher=lambda s: [
                {"userStatus": {"email": "a@example.com"}},
                {"userStatus": {"email": "b@example.com"}},
            ],
        )
        result = probe.probe_identity(pseudonymize_email("b@example.com"), 4242)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_IDENTITY_AMBIGUOUS")

    def test_missing_email_fails_closed(self):
        probe = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (snap(), "OK"),
            user_status_fetcher=lambda s: [{"userStatus": {}}],
        )
        result = probe.probe_identity(pseudonymize_email("b@example.com"), 4242)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_IDENTITY_EMAIL_MISSING")

    def test_restart_waits_for_new_language_server_pid_then_verifies(self):
        before = snap(4242)
        after = snap(4342)
        calls = {"inspect": 0, "restart": 0}

        def provider(hint):
            calls["inspect"] += 1
            if calls["inspect"] == 1:
                return before, "OK"
            return after, "OK"

        def restart_executor(snapshot, timeout):
            calls["restart"] += 1
            self.assertEqual(snapshot.language_server_pid, 4242)
            return True, "DESKTOP_RESTART_LAUNCHED"

        probe = DesktopRuntimeProbe(
            process_snapshot_provider=provider,
            user_status_fetcher=lambda s: [{"userStatus": {"email": "b@example.com"}}],
            restart_executor=restart_executor,
            sleep_func=lambda _: None,
        )
        result = probe.restart_and_verify(
            pseudonymize_email("b@example.com"),
            pid_hint=4242,
            ready_timeout_sec=1.0,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["old_language_server_pid"], 4242)
        self.assertEqual(result["language_server_pid"], 4342)
        self.assertEqual(calls["restart"], 1)

    def test_restart_identity_mismatch_does_not_loop_restart(self):
        before = snap(4242)
        after = snap(4342)
        calls = {"inspect": 0, "restart": 0}

        def provider(hint):
            calls["inspect"] += 1
            return (before, "OK") if calls["inspect"] == 1 else (after, "OK")

        def restart_executor(snapshot, timeout):
            calls["restart"] += 1
            return True, "DESKTOP_RESTART_LAUNCHED"

        probe = DesktopRuntimeProbe(
            process_snapshot_provider=provider,
            user_status_fetcher=lambda s: [{"userStatus": {"email": "a@example.com"}}],
            restart_executor=restart_executor,
            sleep_func=lambda _: None,
        )
        result = probe.restart_and_verify(
            pseudonymize_email("b@example.com"),
            pid_hint=4242,
            ready_timeout_sec=1.0,
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_IDENTITY_MISMATCH")
        self.assertEqual(calls["restart"], 1)

    def test_restart_executor_failure_fails_closed(self):
        probe = DesktopRuntimeProbe(
            process_snapshot_provider=lambda hint: (snap(), "OK"),
            user_status_fetcher=lambda s: [],
            restart_executor=lambda snapshot, timeout: (False, "DESKTOP_RESTART_FAILED"),
            sleep_func=lambda _: None,
        )
        result = probe.restart_and_verify(pseudonymize_email("b@example.com"), 4242)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "DESKTOP_RESTART_FAILED")
        self.assertTrue(result["restart_performed"])


if __name__ == "__main__":
    unittest.main()
