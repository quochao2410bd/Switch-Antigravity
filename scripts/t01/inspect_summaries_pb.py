import os

pb_path = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb")
if os.path.exists(pb_path):
    with open(pb_path, "rb") as f:
        data = f.read()
    print(f"Size: {len(data)} bytes")
    # Printable strings extraction
    import re
    strings = re.findall(rb"[\x20-\x7e]{4,}", data)
    print("Extracted strings in pb:")
    for s in strings:
        try:
            print(" ", s.decode('utf-8'))
        except Exception:
            pass
