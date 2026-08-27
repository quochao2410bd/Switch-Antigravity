#!/usr/bin/env python3
from __future__ import annotations

import production_adapters as pa


class SafeProductionAdapters(pa.ProductionAdapters):
    """Production adapter with explicit T03 post-transition crash reconciliation.

    A post-transition dry-run is always performed before a new send. Existing journal
    decisions are mapped forward without re-dispatching the recovery prompt.
    """

    def resume_conversation(self, event_id: str):
        preflight = self._t03_preflight(event_id)
        decision = preflight.get("decision")

        structural_ok = (
            preflight.get("t03_status") == "DRY_RUN_READ_ONLY_SUCCESS"
            and preflight.get("exact_uuid") is True
            and preflight.get("draft_present") is False
        )
        if not structural_ok:
            return {"status": "POST_TRANSITION_PREFLIGHT_FAILED", "preflight": preflight}

        if decision == "NEW_ATTEMPT_ALLOWED":
            return pa.asyncio.run(pa.execute_resume_pipeline(self._t03_args(event_id, send=True)))

        # Crash/restart reconciliation: never resend when T03 already has durable or DOM evidence.
        if decision == "TURN_ALREADY_ACTIVE":
            return {
                "status": "TURN_STARTED",
                "reconciled_without_send": True,
                "decision": decision,
            }
        if decision == "RESUME_ALREADY_OBSERVED":
            return {
                "status": "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING",
                "reconciled_without_send": True,
                "decision": decision,
            }
        if decision == "PREVIOUS_SUBMISSION_UNCONFIRMED":
            return {
                "status": "DISPATCHED_UNCONFIRMED",
                "reconciled_without_send": True,
                "decision": decision,
            }

        return {"status": "POST_TRANSITION_PREFLIGHT_FAILED", "preflight": preflight}
