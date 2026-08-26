import os
import re

log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

for i, l in enumerate(lines):
    if "RESOURCE_EXHAUSTED" in l:
        print(f"Line {i+1}: {l.strip()}")
