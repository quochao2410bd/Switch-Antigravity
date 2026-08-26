import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))
from quota_detector import detect_historical_events

def main():
    log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if len(sys.argv) > 1:
        log_path = sys.argv[1]
    
    result = detect_historical_events(log_path)
    print(json.dumps(result, indent=2))
    
    if result.get("status") == "HISTORICAL_QUOTA_EVENT_FOUND":
        sys.exit(0)
    elif result.get("status") == "NO_HISTORICAL_QUOTA_EVENT":
        sys.exit(1)
    else:
        sys.exit(2)

if __name__ == "__main__":
    main()
