import os

log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
if os.path.exists(log_path):
    print(f"Log size: {os.path.getsize(log_path)} bytes")
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    print(f"Total lines: {len(lines)}")
    print("\n--- First 15 lines ---")
    for l in lines[:15]:
        print(l.strip())
    print("\n--- Last 25 lines ---")
    for l in lines[-25:]:
        print(l.strip())
