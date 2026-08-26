import os
import re

def search_logs():
    log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
    if not os.path.exists(log_path):
        print("Log file not found")
        return

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    patterns = [
        r"quota",
        r"RESOURCE_EXHAUSTED",
        r"429",
        r"rate limit",
        r"exhaust",
        r"credit",
        r"streamGenerateContent",
        r"loadCodeAssist",
        r"fetchAvailableModels",
        r"UserTier",
        r"error",
        r"fail"
    ]

    for pat in patterns:
        regex = re.compile(pat, re.IGNORECASE)
        matching = [l.strip() for l in lines if regex.search(l)]
        print(f"=== Pattern: '{pat}' ({len(matching)} lines found) ===")
        # show sample lines (max 5)
        for m in matching[:5]:
            print(f"  {m[:160]}")
        print()

if __name__ == "__main__":
    search_logs()
