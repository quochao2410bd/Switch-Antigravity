#!/usr/bin/env python3
"""
Persistent Recovery Journal for Antigravity Resume Adapter (T03 Prototype)

Manages crash-recovery state for conversation resume attempts.
Stores strictly non-sensitive metadata (UUIDs, state, prompt SHA-256, timestamps).
Implements atomic file writes (write temp + atomic replace) and corrupt file quarantine.
"""

import hashlib
import json
import os
import tempfile
import time
import uuid

STATE_NOT_SENT = "NOT_SENT"
STATE_SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
STATE_MESSAGE_OBSERVED = "MESSAGE_OBSERVED"
STATE_TURN_STARTED = "TURN_STARTED"
STATE_TURN_ACTIVE = "TURN_ACTIVE"
STATE_FAILED = "FAILED"

VALID_STATES = {
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED
}

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

class RecoveryJournal:
    def __init__(self, journal_path=None):
        self.journal_path = journal_path or get_default_journal_path()

    def _read_raw(self):
        """Read and validate journal file. Returns (data_dict, status_str)."""
        if not os.path.exists(self.journal_path):
            return {"version": 1, "records": {}}, "NOT_FOUND"

        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "records" not in data:
                raise ValueError("Malformed journal schema: missing 'records' key")
            return data, "OK"
        except Exception as e:
            corrupt_path = f"{self.journal_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.journal_path, corrupt_path)
            except Exception:
                pass
            return {"version": 1, "records": {}, "corrupted_backup": corrupt_path, "error": str(e)}, "CORRUPTED"

    def _write_atomic(self, data):
        """Atomically persist data using temporary file and atomic replace."""
        target_dir = os.path.dirname(self.journal_path)
        os.makedirs(target_dir, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix="t03_journal_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.journal_path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def get_latest_record(self, conversation_uuid):
        """Retrieve latest recovery record for given conversation UUID."""
        data, status = self._read_raw()
        records = data.get("records", {})
        convo_records = records.get(conversation_uuid.lower(), [])
        if not convo_records:
            return None, status
        return convo_records[-1], status

    def start_recovery_attempt(self, conversation_uuid, prompt_text):
        """Initialize and record a new recovery attempt in NOT_SENT state."""
        data, _ = self._read_raw()
        attempt_id = str(uuid.uuid4())
        prompt_sha = hash_prompt(prompt_text)
        now = time.time()

        record = {
            "attempt_id": attempt_id,
            "conversation_uuid": conversation_uuid.lower(),
            "state": STATE_NOT_SENT,
            "prompt_sha256": prompt_sha,
            "created_at_utc": now,
            "updated_at_utc": now,
            "history": [
                {"state": STATE_NOT_SENT, "timestamp": now}
            ]
        }

        key = conversation_uuid.lower()
        if key not in data["records"]:
            data["records"][key] = []
        data["records"][key].append(record)

        self._write_atomic(data)
        return record

    def transition_state(self, conversation_uuid, attempt_id, new_state, detail=None):
        """Update the state of an existing recovery attempt."""
        if new_state not in VALID_STATES:
            raise ValueError(f"Invalid recovery state: {new_state}")

        data, _ = self._read_raw()
        key = conversation_uuid.lower()
        records = data.get("records", {}).get(key, [])

        target_record = None
        for r in reversed(records):
            if r.get("attempt_id") == attempt_id:
                target_record = r
                break

        if not target_record:
            raise KeyError(f"Recovery attempt {attempt_id} not found for {conversation_uuid}")

        now = time.time()
        target_record["state"] = new_state
        target_record["updated_at_utc"] = now
        hist_entry = {"state": new_state, "timestamp": now}
        if detail:
            hist_entry["detail"] = detail
        target_record["history"].append(hist_entry)

        self._write_atomic(data)
        return target_record
