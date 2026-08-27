#!/usr/bin/env python3
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from supervisor import ActiveEvent, EventStage, RuntimeState, RuntimeStateStore, SwitchSupervisor


class HardenedSwitchSupervisor(SwitchSupervisor):
    """Supervisor hardening required by the accepted T01 integration contract.

    - A logical supervisor session survives watchdog process restarts by reusing the
      durable state session ID. This preserves the incremental log cursor across a
      crash instead of blindly rebaselining at EOF and dropping events.
    - Processed quota events are keyed by session + file identity + raw event ID so
      an inode/file-index reuse after a later rebaseline cannot create a false
      global duplicate.
    """

    def __init__(self, config, adapters, session_id: Optional[str] = None):
        if session_id is None:
            prior = RuntimeStateStore(config.state_path).load()
            if prior.supervisor_session_id:
                session_id = prior.supervisor_session_id
        super().__init__(config, adapters, session_id=session_id)

    def _event_dedupe_key(self, state: RuntimeState, event_id: str) -> str:
        baseline = state.baseline or {}
        identity = baseline.get("file_identity") or {}
        required = ("dev", "ino", "ctime_ns")
        if any(type(identity.get(k)) is not int for k in required):
            raise RuntimeError("BASELINE_IDENTITY_MISSING_FOR_DEDUPE")
        bound_session = baseline.get("supervisor_session_id") or state.supervisor_session_id or self.session_id
        if not isinstance(bound_session, str) or not bound_session:
            raise RuntimeError("BASELINE_SESSION_MISSING_FOR_DEDUPE")
        return (
            f"{bound_session}:"
            f"{identity['dev']}:{identity['ino']}:{identity['ctime_ns']}:"
            f"{event_id}"
        )

    def _record_new_event(self, state: RuntimeState, poll: Dict[str, Any]) -> bool:
        event = poll.get("latest_event") or {}
        event_id = event.get("event_id")
        if not event_id:
            raise RuntimeError("QUOTA_EVENT_ID_MISSING")
        dedupe_key = self._event_dedupe_key(state, event_id)

        # Raw IDs are accepted only as a conservative legacy tombstone from an
        # earlier integration build. New writes always use scoped keys.
        if dedupe_key in state.processed_event_ids or event_id in state.processed_event_ids:
            self._commit_cursor(state, int(poll.get("cursor", state.baseline.get("committed_byte_offset", 0))))
            self.store.save(state)
            return False

        if state.active_event:
            active_key = state.active_event.get("dedupe_key")
            if active_key is None and state.active_event.get("event_id") == event_id:
                state.active_event["dedupe_key"] = dedupe_key
                active_key = dedupe_key
            if active_key != dedupe_key:
                raise RuntimeError("MULTIPLE_ACTIVE_QUOTA_EVENTS_BLOCKED")

        if not state.active_event:
            active = asdict(ActiveEvent(
                event_id=event_id,
                detected_at_epoch=time.time(),
                source_cursor=int(poll.get("cursor", 0)),
                last_status="QUOTA_CONFIRMED",
            ))
            active["dedupe_key"] = dedupe_key
            state.active_event = active

        self._commit_cursor(state, int(poll.get("cursor", 0)))
        self.store.save(state)
        return True

    def _finalize_event(self, state: RuntimeState, status: str) -> Dict[str, Any]:
        event = state.active_event or {}
        event_id = event.get("event_id")
        dedupe_key = event.get("dedupe_key")
        if event_id and not dedupe_key:
            dedupe_key = self._event_dedupe_key(state, event_id)
            event["dedupe_key"] = dedupe_key

        if dedupe_key and dedupe_key not in state.processed_event_ids:
            state.processed_event_ids.append(dedupe_key)
            state.processed_event_ids = state.processed_event_ids[-self.config.max_processed_events:]

        if state.active_event:
            state.active_event["stage"] = EventStage.COMPLETE.value
            state.active_event["last_status"] = status
        self.store.save(state)
        summary = self._sanitized_event_summary(state.active_event)
        state.active_event = None
        self.store.save(state)
        return {"status": status, "event": summary}
