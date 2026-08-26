import os
import glob

brain_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\brain")
for convo_id in os.listdir(brain_dir):
    cpath = os.path.join(brain_dir, convo_id)
    if os.path.isdir(cpath):
        print(f"\nConvo Brain dir: {convo_id}")
        for root, dirs, files in os.walk(cpath):
            rel_root = os.path.relpath(root, cpath)
            for f in files:
                fpath = os.path.join(root, f)
                print(f"  {os.path.join(rel_root, f)} ({os.path.getsize(fpath)} bytes)")
