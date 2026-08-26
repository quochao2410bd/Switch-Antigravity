import os
import re
import sys
import json
import argparse

INDIVIDUAL_QUOTA_PATTERN = re.compile(
    r'(?:ERROR: logging before google\.Init:\s+[IE](?P<timestamp>\d{4}\s+[\d:.]+)\s+(?P<pid>\d+)\s+(?P<source>[\w.]+:\d+)\]\s+)?'
    r'.*?(?:agent executor error:\s+)?(?:calling model:\s+)?'
    r'RESOURCE_EXHAUSTED\s+\(code\s+(?P<code>\d+)\):\s+'
    r'Individual quota reached\.\s+Please upgrade your subscription to increase your limits\.\s+'
    r'Resets in\s+(?P<resets_in>[^.)]+)',
    re.IGNORECASE
)

def detect_quota_from_content(content: str) -> dict:
    lines = content.splitlines()
    matches = []
    
    for line_idx, line in enumerate(lines, 1):
        m = INDIVIDUAL_QUOTA_PATTERN.search(line)
        if m:
            matches.append({
                "line_number": line_idx,
                "timestamp": m.group("timestamp") if "timestamp" in m.groupdict() else None,
                "pid": m.group("pid") if "pid" in m.groupdict() else None,
                "source": m.group("source") if "source" in m.groupdict() else None,
                "code": int(m.group("code")) if "code" in m.groupdict() and m.group("code") else 429,
                "resets_in": m.group("resets_in").strip() if "resets_in" in m.groupdict() and m.group("resets_in") else None,
                "raw_line": line.strip()
            })

    if matches:
        latest = matches[-1]
        return {
            "quota_exhausted": True,
            "confidence": 1.0,
            "error_code": latest["code"],
            "error_type": "RESOURCE_EXHAUSTED_INDIVIDUAL_QUOTA",
            "error_message": "Individual quota reached. Please upgrade your subscription to increase your limits.",
            "resets_in": latest["resets_in"],
            "latest_timestamp": latest["timestamp"],
            "total_matches": len(matches),
            "events": matches
        }
    else:
        return {
            "quota_exhausted": False,
            "confidence": 0.0,
            "error_code": None,
            "error_type": None,
            "error_message": None,
            "resets_in": None,
            "latest_timestamp": None,
            "total_matches": 0,
            "events": []
        }

def detect_quota_from_file(file_path: str) -> dict:
    if not os.path.exists(file_path):
        return {
            "quota_exhausted": False,
            "error": f"File not found: {file_path}",
            "confidence": 0.0
        }
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return detect_quota_from_content(content)

def main():
    parser = argparse.ArgumentParser(description="Antigravity Desktop Quota Failure Detector")
    parser.add_argument("--file", help="Path to log file to inspect", default=None)
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    log_path = args.file
    if not log_path:
        log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")

    result = detect_quota_from_file(log_path)
    if args.json or True: # default to JSON for machine checkability
        print(json.dumps(result, indent=2))

    if result.get("quota_exhausted"):
        sys.exit(0)
    elif "error" in result:
        sys.exit(2)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
