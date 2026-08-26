import sqlite3
import glob
import os

convos_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\conversations")
for db_file in glob.glob(os.path.join(convos_dir, "*.db")):
    conn = sqlite3.connect(f"file:{os.path.abspath(db_file)}?mode=ro", uri=True)
    c = conn.cursor()
    c.execute("SELECT idx, step_type, status, error_details, render_info FROM steps WHERE error_details IS NOT NULL OR status != 3")
    rows = c.fetchall()
    if rows:
        print(f"\nDB: {os.path.basename(db_file)} (found {len(rows)} matching steps)")
        for idx, stype, status, err, rinfo in rows[:10]:
            err_str = repr(err[:100]) if err else "None"
            rinfo_str = repr(rinfo[:100]) if rinfo else "None"
            print(f"  Step {idx}: type={stype}, status={status}, err={err_str}, rinfo={rinfo_str}")
    conn.close()
