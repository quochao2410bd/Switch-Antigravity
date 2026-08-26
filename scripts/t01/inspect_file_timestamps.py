import os
import glob
import datetime

convos_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\conversations")
for f in glob.glob(os.path.join(convos_dir, "*.db")):
    mtime = os.path.getmtime(f)
    ctime = os.path.getctime(f)
    print(f"File: {os.path.basename(f):<45} Created: {datetime.datetime.fromtimestamp(ctime)} Modified: {datetime.datetime.fromtimestamp(mtime)}")
