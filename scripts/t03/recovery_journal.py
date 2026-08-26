#!/usr/bin/env python3
"""
Persistent Recovery Journal for Antigravity Resume Adapter (T03 Prototype)

Manages crash-recovery state for conversation resume attempts.
Stores strictly non-sensitive metadata (UUIDs, state, prompt SHA-256, timestamps).
Implements strict state transition graph, durability barriers (flush + fsync + atomic replace),
quarantine of corrupted journals, and deterministic recovery permission evaluation.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid

STATE_NOT_SENT = "NOT_SENT"
STATE_SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
STATE_DISPATCHED_UNCONFIRMED = "DISPATCHED_UNCONFIRMED"
STATE_MESSAGE_OBSERVED = "MESSAGE_OBSERVED"
STATE_TURN_STARTED = "TURN_STARTED"
STATE_TURN_ACTIVE = "TURN_ACTIVE"
STATE_FAILED = "FAILED"

VALID_STATES = {
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_DISPATCHED_UNCONFIRMED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED
}

# Strict allowed state transitions graph
ALLOWED_TRANSITIONS = {
    STATE_NOT_SENT: {STATE_SUBMISSION_ATTEMPTED, STATE_FAILED},
    STATE_SUBMISSION_ATTEMPTED: {STATE_MESSAGE_OBSERVED, STATE_DISPATCHED_UNCONFIRMED, STATE_FAILED},
    STATE_DISPATCHED_UNCONFIRMED: {STATE_MESSAGE_OBSERVED, STATE_FAILED},
    STATE_MESSAGE_OBSERVED: {STATE_TURN_STARTED, STATE_FAILED},
    STATE_TURN_STARTED: {STATE_TURN_ACTIVE, STATE_FAILED},
    STATE_TURN_ACTIVE: {STATE_FAILED},
    STATE_FAILED: set()
}

DECISION_NEW_ATTEMPT_ALLOWED = "NEW_ATTEMPT_ALLOWED"
DECISION_RESUME_ALREADY_OBSERVED = "RESUME_ALREADY_OBSERVED"
DECISION_TURN_ALREADY_ACTIVE = "TURN_ALREADY_ACTIVE"
DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED = "PREVIOUS_SUBMISSION_UNCONFIRMED"
DECISION_RECOVERY_STATE_UNKNOWN = "RECOVERY_STATE_UNKNOWN"
DECISION_JOURNAL_CORRUPTED = "JOURNAL_CORRUPTED"
DECISION_MANUAL_RECONCILIATION_REQUIRED = "MANUAL_RECONCILIATION_REQUIRED"
DECISION_BLOCKED_DRAFT_PRESENT = "BLOCKED_DRAFT_PRESENT"

UUID_REGEX = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

def validate_uuid(convo_uuid):
    if not convo_uuid or not isinstance(convo_uuid, str) or not UUID_REGEX.match(convo_uuid.strip()):
        raise ValueError(f"Invalid UUID format: {convo_uuid}")
    return convo_uuid.strip().lower()

def get_default_journal_path():
    """Return platform-safe location for recovery journal."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base_dir = os.path.join(local_appdata, "SwitchAntigravity")
    else:
        base_dir = os.path.join(os.path.expanduser("~"), ".switch_antigravity")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "t03_recovery_journal.json")

def hash_prompt(prompt_text):
    """Compute SHA-256 hash of normalized prompt text."""
    if not prompt_text:
        return ""
    normalized = " ".join(prompt_text.strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def evaluate_recovery_permission(latest_record, live_dom_state, prompt_hash, journal_status="OK", is_first_attempt=False):
    """
    Deterministic evaluation of recovery permission.
    Returns (decision_code, explanation).
    """
    if journal_status == "CORRUPTED":
        return DECISION_JOURNAL_CORRUPTED, "Recovery journal is corrupted. Fail closed to prevent duplicate submission."

    if not live_dom_state or not isinstance(live_dom_state, dict):
        return DECISION_RECOVERY_STATE_UNKNOWN, "Live DOM state could not be inspected."

    if live_dom_state.get("isMainTurnActive"):
        return DECISION_TURN_ALREADY_ACTIVE, "Target conversation has an active turn executing (Stop button present)."

    if live_dom_state.get("draftPresent"):
        return DECISION_BLOCKED_DRAFT_PRESENT, "Composer contains an existing unsubmitted user draft. Overwriting refused."

    dom_duplicate_status = live_dom_state.get("duplicateStatus")
    last_user_hash = live_dom_state.get("lastUserMessageHash")

    if dom_duplicate_status == "RESUME_MESSAGE_PRESENT" or (last_user_hash and last_user_hash == prompt_hash):
        return DECISION_RESUME_ALREADY_OBSERVED, "Intended resume prompt is already present in conversation history."

    if latest_record:
        prev_state = latest_record.get("state")
        prev_hash = latest_record.get("prompt_sha256")
        same_prompt = (prev_hash == prompt_hash)

        if prev_state in [STATE_SUBMISSION_ATTEMPTED, STATE_DISPATCHED_UNCONFIRMED]:
            if same_prompt and last_user_hash == prompt_hash:
                return DECISION_RESUME_ALREADY_OBSERVED, "Previous attempt was submitted and confirmed in DOM."
            return DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED, (
                f"Previous recovery attempt was in state '{prev_state}'. "
                "DOM state is unconfirmed. Blind resend is strictly blocked."
            )

        if prev_state == STATE_MESSAGE_OBSERVED:
            return DECISION_RESUME_ALREADY_OBSERVED, "Previous recovery message was already confirmed observed in DOM."

        if prev_state in [STATE_TURN_STARTED, STATE_TURN_ACTIVE]:
            return DECISION_TURN_ALREADY_ACTIVE, f"Previous recovery turn was recorded in active state '{prev_state}'."

        if prev_state == STATE_FAILED:
            failure_stage = latest_record.get("failure_stage", "UNKNOWN")
            if failure_stage == "POST_IRREVERSIBLE_UNKNOWN":
                return DECISION_MANUAL_RECONCILIATION_REQUIRED, "Previous attempt failed after input dispatch with unconfirmed outcome."

    if dom_duplicate_status == "DUPLICATE_STATE_UNKNOWN":
        if is_first_attempt and not latest_record and live_dom_state.get("isConversationEmptyOrIdle"):
            return DECISION_NEW_ATTEMPT_ALLOWED, "First attempt on proven idle conversation with clean journal."
        return DECISION_RECOVERY_STATE_UNKNOWN, "Conversation duplicate status is unknown. Fail closed."

    if dom_duplicate_status == "RESUME_NOT_PRESENT":
        return DECISION_NEW_ATTEMPT_ALLOWED, "Verified target is idle, no draft, and resume prompt not present in history."

    return DECISION_RECOVERY_STATE_UNKNOWN, f"Unhandled duplicate status '{dom_duplicate_status}'. Fail closed."

class RecoveryJournal:
    def __init__(self, journal_path=None):
        self.journal_path = journal_path or get_default_journal_path()
        self._is_corrupted = False

    def _read_raw(self):
        """Read and validate journal file. Returns (data_dict, status_str)."""
        if self._is_corrupted:
            return {"version": 2, "records": {}, "error": "Journal is marked corrupted"}, "CORRUPTED"

        if not os.path.exists(self.journal_path):
            return {"version": 2, "records": {}}, "NOT_FOUND"

        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "records" not in data:
                raise ValueError("Malformed journal schema: missing 'records' key")
            return data, "OK"
        except Exception as e:
            self._is_corrupted = True
            corrupt_path = f"{self.journal_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.journal_path, corrupt_path)
            except Exception:
                pass
            return {"version": 2, "records": {}, "corrupted_backup": corrupt_path, "error": str(e)}, "CORRUPTED"

    def _write_atomic(self, data):
        """
        Durably persist data using temp file, flush, fsync, and atomic replace.
        """
        target_dir = os.path.dirname(self.journal_path)
        os.makedirs(target_dir, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix="t03_journal_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(temp_path, self.journal_path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def get_latest_record(self, conversation_uuid):
        """Retrieve latest recovery record for given conversation UUID."""
        val_uuid = validate_uuid(conversation_uuid)
        data, status = self._read_raw()
        if status == "CORRUPTED":
            return None, "CORRUPTED"
        records = data.get("records", {})
        convo_records = records.get(val_uuid, [])
        if not convo_records:
            return None, status
        return convo_records[-1], status

    def start_recovery_attempt(self, conversation_uuid, prompt_text):
        """
        Initialize and record a new recovery attempt in NOT_SENT state.
        Refuses to start if journal is corrupted.
        """
        val_uuid = validate_uuid(conversation_uuid)
        data, status = self._read_raw()
        if status == "CORRUPTED":
            raise RuntimeError("Cannot start recovery attempt: recovery journal is corrupted. Manual reconciliation required.")

        attempt_id = str(uuid.uuid4())
        prompt_sha = hash_prompt(prompt_text)
        now = time.time()

        record = {
            "attempt_id": attempt_id,
            "conversation_uuid": val_uuid,
            "state": STATE_NOT_SENT,
            "prompt_sha256": prompt_sha,
            "created_at_utc": now,
            "updated_at_utc": now,
            "failure_stage": None,
            "history": [
                {"state": STATE_NOT_SENT, "timestamp": now}
            ]
        }

        if val_uuid not in data["records"]:
            data["records"][val_uuid] = []
        data["records"][val_uuid].append(record)

        self._write_atomic(data)
        return record

    def transition_state(self, conversation_uuid, attempt_id, new_state, failure_stage=None, detail=None):
        """
        Update the state of an existing recovery attempt enforcing allowed transition graph.
        """
        val_uuid = validate_uuid(conversation_uuid)
        if new_state not in VALID_STATES:
            raise ValueError(f"Invalid recovery state: {new_state}")

        data, status = self._read_raw()
        if status == "CORRUPTED":
            raise RuntimeError("Cannot transition state: recovery journal is corrupted.")

        records = data.get("records", {}).get(val_uuid, [])
        target_record = None
        for r in reversed(records):
            if r.get("attempt_id") == attempt_id:
                target_record = r
                break

        if not target_record:
            raise KeyError(f"Recovery attempt {attempt_id} not found for {val_uuid}")

        current_state = target_record["state"]
        allowed = ALLOWED_TRANSITIONS.get(current_state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal state transition from '{current_state}' to '{new_state}'. "
                f"Allowed transitions: {list(allowed)}"
            )

        now = time.time()
        target_record["state"] = new_state
        target_record["updated_at_utc"] = now
        if failure_stage:
            target_record["failure_stage"] = failure_stage
        hist_entry = {"state": new_state, "timestamp": now}
        if detail:
            hist_entry["detail"] = detail
        target_record["history"].append(hist_entry)

        self._write_atomic(data)
        return target_record
