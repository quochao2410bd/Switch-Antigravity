#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

STATE_SCHEMA_VERSION = 1


class EventStage(str, Enum):
    DETECTED = "DETECTED"
    DISCOVERING = "DISCOVERING"
    SWITCHING = "SWITCHING"
    VERIFYING_DESKTOP = "VERIFYING_DESKTOP"
    REBASELINING = "REBASELINING"
    RESUMING = "RESUMING"
    WAITING_PROGRESS = "WAITING_PROGRESS"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMPLETE = "COMPLETE"
    FAILED_SAFE = "FAILED_SAFE"


@dataclass
class SupervisorConfig:
    log_path: str
    conversation_uuid: str
    expected_agm_sha256: str
    language_server_pid: int
    state_path: str
    t03_journal_path: str
    resume_prompt: str
    model_group: str = "gemini-pro"
    min_quota_pct: int = 20
    max_rotation_attempts: int = 3
    poll_interval_sec: float = 2.0
    max_processed_events: int = 200


@dataclass
class ActiveEvent:
    event_id: str
    detected_at_epoch: float
    stage: str = EventStage.DETECTED.value
    source_cursor: int = 0
    current_account_ref: Optional[str] = None
    candidate_account_ref: Optional[str] = None
    rotation_count: int = 0
    failure_codes: List[str] = field(default_factory=list)
    last_status: str = ""


@dataclass
class RuntimeState:
    schema_version: int = STATE_SCHEMA_VERSION
    supervisor_session_id: str = ""
    baseline: Optional[Dict[str, Any]] = None
    active_event: Optional[Dict[str, Any]] = None
    processed_event_ids: List[str] = field(default_factory=list)
    updated_at_epoch: float = 0.0


class AdapterContract(Protocol):
    def create_quota_baseline(self, session_id: str, ls_pid: int) -> Dict[str, Any]: ...
    def poll_quota(self, baseline: Dict[str, Any], session_id: str, ls_pid: int) -> Dict[str, Any]: ...
    def current_ls_pid(self) -> int: ...
    def get_current_account(self) -> Dict[str, Any]: ...
    def discover_candidates(self, session_id: str, current_account: str) -> List[Dict[str, Any]]: ...
    def switch_account(self, account: str) -> Dict[str, Any]: ...
    def desktop_adoption_verifier_available(self) -> bool: ...
    def verify_desktop_adoption(self, expected_account_ref: str) -> Dict[str, Any]: ...
    def resume_conversation(self) -> Dict[str, Any]: ...
    def probe_resume_progress(self) -> Dict[str, Any]: ...


class RuntimeStateStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    def load(self) -> RuntimeState:
        if not os.path.exists(self.path):
            return RuntimeState(updated_at_epoch=time.time())
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise RuntimeError("SUPERVISOR_STATE_SCHEMA_UNSUPPORTED")
        return RuntimeState(**raw)

    def save(self, state: RuntimeState) -> None:
        state.updated_at_epoch = time.time()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="switch_antigravity_", suffix=".tmp", dir=parent or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(state), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise


class SwitchSupervisor:
    def __init__(self, config: SupervisorConfig, adapters: AdapterContract, session_id: Optional[str] = None):
        self.config = config
        self.adapters = adapters
        self.store = RuntimeStateStore(config.state_path)
        self.session_id = session_id or f"sup_{uuid.uuid4()}"

    @staticmethod
    def _ref_for(account: str) -> str:
        import hashlib
        return "acc_" + hashlib.sha256(account.strip().lower().encode("utf-8")).hexdigest()[:12]

    def _load_state(self) -> RuntimeState:
        state = self.store.load()
        if state.supervisor_session_id != self.session_id:
            state.supervisor_session_id = self.session_id
            state.baseline = None
            self.store.save(state)
        return state

    def _ensure_baseline(self, state: RuntimeState) -> Dict[str, Any]:
        if state.baseline is not None:
            return state.baseline
        pid = self.adapters.current_ls_pid()
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("LANGUAGE_SERVER_PID_UNAVAILABLE")
        baseline = self.adapters.create_quota_baseline(self.session_id, pid)
        if baseline.get("status") != "BASELINE_INITIALIZED":
            raise RuntimeError(f"BASELINE_INIT_FAILED:{baseline.get('status')}")
        if baseline.get("language_server_process_id") != pid:
            raise RuntimeError("BASELINE_PID_BINDING_MISSING")
        if baseline.get("supervisor_session_id") != self.session_id:
            raise RuntimeError("BASELINE_SESSION_BINDING_MISSING")
        state.baseline = baseline
        self.store.save(state)
        return baseline

    def _commit_cursor(self, state: RuntimeState, cursor: int) -> None:
        if state.baseline is None:
            raise RuntimeError("BASELINE_REQUIRED")
        state.baseline["committed_byte_offset"] = int(cursor)
        state.baseline["file_size"] = int(cursor)

    def _record_new_event(self, state: RuntimeState, poll: Dict[str, Any]) -> None:
        event = poll.get("latest_event") or {}
        event_id = event.get("event_id")
        if not event_id:
            raise RuntimeError("QUOTA_EVENT_ID_MISSING")
        if event_id in state.processed_event_ids:
            self._commit_cursor(state, int(poll.get("cursor", state.baseline.get("committed_byte_offset", 0))))
            self.store.save(state)
            return
        if state.active_event and state.active_event.get("event_id") != event_id:
            raise RuntimeError("MULTIPLE_ACTIVE_QUOTA_EVENTS_BLOCKED")
        if not state.active_event:
            active = ActiveEvent(
                event_id=event_id,
                detected_at_epoch=time.time(),
                source_cursor=int(poll.get("cursor", 0)),
                last_status="QUOTA_CONFIRMED",
            )
            state.active_event = asdict(active)
        self._commit_cursor(state, int(poll.get("cursor", 0)))
        self.store.save(state)

    def _finalize_event(self, state: RuntimeState, status: str) -> Dict[str, Any]:
        event = state.active_event or {}
        event_id = event.get("event_id")
        if event_id and event_id not in state.processed_event_ids:
            state.processed_event_ids.append(event_id)
            state.processed_event_ids = state.processed_event_ids[-self.config.max_processed_events:]
        if state.active_event:
            state.active_event["stage"] = EventStage.COMPLETE.value
            state.active_event["last_status"] = status
        self.store.save(state)
        summary = self._sanitized_event_summary(state.active_event)
        state.active_event = None
        self.store.save(state)
        return {"status": status, "event": summary}

    @staticmethod
    def _sanitized_event_summary(event: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not event:
            return None
        return {
            "event_id": event.get("event_id"),
            "stage": event.get("stage"),
            "current_account_ref": event.get("current_account_ref"),
            "candidate_account_ref": event.get("candidate_account_ref"),
            "rotation_count": event.get("rotation_count", 0),
            "failure_codes": list(event.get("failure_codes", [])),
            "last_status": event.get("last_status"),
        }

    def _resolve_candidate(self, candidate_ref: str, current_account: str) -> Optional[Dict[str, Any]]:
        for item in self.adapters.discover_candidates(self.session_id, current_account):
            if item.get("account_ref") == candidate_ref:
                return item
        return None

    def _process_active_event(self, state: RuntimeState) -> Dict[str, Any]:
        if not state.active_event:
            return {"status": "NO_ACTIVE_EVENT"}

        event = state.active_event
        while True:
            stage = EventStage(event["stage"])

            if stage == EventStage.DETECTED:
                cur = self.adapters.get_current_account()
                if not cur.get("verified") or not cur.get("account"):
                    event["last_status"] = "BLOCKED_CURRENT_ACCOUNT_UNVERIFIED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                event["current_account_ref"] = cur.get("account_ref") or self._ref_for(cur["account"])
                event["stage"] = EventStage.DISCOVERING.value
                event["last_status"] = "CURRENT_ACCOUNT_VERIFIED"
                self.store.save(state)
                continue

            if stage == EventStage.DISCOVERING:
                if event.get("rotation_count", 0) >= self.config.max_rotation_attempts:
                    event["stage"] = EventStage.FAILED_SAFE.value
                    event["last_status"] = "BLOCKED_ROTATION_EXHAUSTED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}

                cur = self.adapters.get_current_account()
                if not cur.get("verified") or not cur.get("account"):
                    event["last_status"] = "BLOCKED_CURRENT_ACCOUNT_UNVERIFIED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}

                candidates = self.adapters.discover_candidates(self.session_id, cur["account"])
                excluded = set(event.get("excluded_account_refs", []))
                selected = next((x for x in candidates if x.get("eligible") and x.get("account_ref") not in excluded), None)
                if not selected:
                    event["last_status"] = "BLOCKED_NO_ELIGIBLE_ACCOUNT"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                event["candidate_account_ref"] = selected["account_ref"]
                event["stage"] = EventStage.SWITCHING.value
                event["last_status"] = "CANDIDATE_SELECTED"
                self.store.save(state)
                continue

            if stage == EventStage.SWITCHING:
                if not self.adapters.desktop_adoption_verifier_available():
                    event["last_status"] = "BLOCKED_DESKTOP_ADOPTION_VERIFIER_UNAVAILABLE"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                cur = self.adapters.get_current_account()
                if not cur.get("verified") or not cur.get("account"):
                    event["last_status"] = "BLOCKED_CURRENT_ACCOUNT_UNVERIFIED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                candidate = self._resolve_candidate(event["candidate_account_ref"], cur["account"])
                if not candidate:
                    event["stage"] = EventStage.DISCOVERING.value
                    event["candidate_account_ref"] = None
                    event["last_status"] = "CANDIDATE_NO_LONGER_AVAILABLE"
                    self.store.save(state)
                    continue
                sw = self.adapters.switch_account(candidate["account"])
                if not sw.get("verified"):
                    event["rotation_count"] = int(event.get("rotation_count", 0)) + 1
                    event.setdefault("failure_codes", []).append(sw.get("error_code") or "SWITCH_VERIFY_FAILED")
                    event.setdefault("excluded_account_refs", []).append(candidate["account_ref"])
                    event["candidate_account_ref"] = None
                    event["stage"] = EventStage.DISCOVERING.value
                    event["last_status"] = "SWITCH_FAILED_RETRYING"
                    self.store.save(state)
                    continue
                event["stage"] = EventStage.VERIFYING_DESKTOP.value
                event["last_status"] = "CREDENTIAL_SWITCH_VERIFIED"
                self.store.save(state)
                continue

            if stage == EventStage.VERIFYING_DESKTOP:
                adoption = self.adapters.verify_desktop_adoption(event["candidate_account_ref"])
                if not adoption.get("verified"):
                    event["last_status"] = adoption.get("status") or "BLOCKED_DESKTOP_ADOPTION_UNVERIFIED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                event["stage"] = EventStage.REBASELINING.value
                event["last_status"] = "DESKTOP_ADOPTION_VERIFIED"
                self.store.save(state)
                continue

            if stage == EventStage.REBASELINING:
                pid = self.adapters.current_ls_pid()
                baseline = self.adapters.create_quota_baseline(self.session_id, pid)
                if baseline.get("status") != "BASELINE_INITIALIZED" or baseline.get("language_server_process_id") != pid or baseline.get("supervisor_session_id") != self.session_id:
                    event["last_status"] = "BLOCKED_REBASELINE_FAILED"
                    self.store.save(state)
                    return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}
                state.baseline = baseline
                event["stage"] = EventStage.RESUMING.value
                event["last_status"] = "REBASELINED_AFTER_ACCOUNT_TRANSITION"
                self.store.save(state)
                continue

            if stage == EventStage.RESUMING:
                resume = self.adapters.resume_conversation()
                rstatus = resume.get("status")
                if rstatus == "TURN_STARTED":
                    return self._finalize_event(state, "RECOVERY_COMPLETE")
                if rstatus == "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING":
                    event["stage"] = EventStage.WAITING_PROGRESS.value
                    event["last_status"] = rstatus
                    self.store.save(state)
                    return {"status": rstatus, "event": self._sanitized_event_summary(event)}
                if rstatus == "DISPATCHED_UNCONFIRMED":
                    event["stage"] = EventStage.WAITING_CONFIRMATION.value
                    event["last_status"] = rstatus
                    self.store.save(state)
                    return {"status": rstatus, "event": self._sanitized_event_summary(event)}
                event["stage"] = EventStage.FAILED_SAFE.value
                event["last_status"] = f"RESUME_FAILED:{rstatus or 'UNKNOWN'}"
                self.store.save(state)
                return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}

            if stage == EventStage.WAITING_PROGRESS:
                probe = self.adapters.probe_resume_progress()
                if probe.get("verified"):
                    return self._finalize_event(state, "RECOVERY_COMPLETE")
                event["last_status"] = probe.get("status") or "WAITING_PROGRESS"
                self.store.save(state)
                return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}

            if stage == EventStage.WAITING_CONFIRMATION:
                event["last_status"] = "MANUAL_RECONCILIATION_REQUIRED_AFTER_UNCONFIRMED_DISPATCH"
                self.store.save(state)
                return {"status": event["last_status"], "event": self._sanitized_event_summary(event)}

            if stage == EventStage.FAILED_SAFE:
                return {"status": event.get("last_status") or "FAILED_SAFE", "event": self._sanitized_event_summary(event)}

            if stage == EventStage.COMPLETE:
                return self._finalize_event(state, "RECOVERY_COMPLETE")

    def run_once(self) -> Dict[str, Any]:
        state = self._load_state()
        try:
            self._ensure_baseline(state)
        except Exception as e:
            return {"status": "BASELINE_SETUP_FAILED", "error_code": str(e)}

        if state.active_event:
            return self._process_active_event(state)

        pid = self.adapters.current_ls_pid()
        poll = self.adapters.poll_quota(state.baseline, self.session_id, pid)
        status = poll.get("status")

        if status == "BASELINE_INVALID":
            state.baseline = None
            self.store.save(state)
            try:
                self._ensure_baseline(state)
                return {"status": "REBASELINED_AFTER_INVALID_BASELINE"}
            except Exception as e:
                return {"status": "BASELINE_SETUP_FAILED", "error_code": str(e)}

        if status == "NO_NEW_EVENT":
            self._commit_cursor(state, int(poll.get("cursor", state.baseline.get("committed_byte_offset", 0))))
            self.store.save(state)
            return {"status": "IDLE"}

        if status != "NEW_CONFIRMED_QUOTA_EVENT":
            return {"status": "POLL_FAILED_SAFE", "poll_status": status}

        self._record_new_event(state, poll)
        return self._process_active_event(state)

    def run_forever(self) -> None:
        while True:
            result = self.run_once()
            print(json.dumps(result, sort_keys=True))
            time.sleep(max(0.2, self.config.poll_interval_sec))
