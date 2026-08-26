import sqlite3
import os

db_path = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\conversations\4674ef3b-d559-4a90-87e2-c30b11f03250.db")
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
c = conn.cursor()

print("--- trajectory_metadata_blob ---")
c.execute("SELECT id, data FROM trajectory_metadata_blob")
for row in c.fetchall():
    print(f"id: {row[0]}, data len: {len(row[1]) if row[1] else 0}")
    # try printing partial text/bytes
    data = row[1]
    if data:
        print("sample bytes (ascii/repr):", repr(data[:200]))

print("\n--- steps count and last step ---")
c.execute("SELECT idx, step_type, status, has_subtrajectory FROM steps ORDER BY idx DESC LIMIT 5")
for row in c.fetchall():
    print(f"step idx: {row[0]}, step_type: {row[1]}, status: {row[2]}, has_subtrajectory: {row[3]}")

conn.close()
