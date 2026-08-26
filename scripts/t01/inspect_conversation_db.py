import sqlite3
import glob
import os
import sys

def inspect_convos():
    convos_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\conversations")
    db_files = glob.glob(os.path.join(convos_dir, "*.db"))
    print(f"Found {len(db_files)} db files in {convos_dir}")

    for db_file in db_files:
        print(f"\n==========================================")
        print(f"DB: {os.path.basename(db_file)}")
        print(f"Size: {os.path.getsize(db_file)} bytes")
        try:
            # Read-only URI connection
            uri = f"file:{os.path.abspath(db_file)}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            cursor = conn.cursor()
            cursor.execute("SELECT type, name, sql FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            for t_type, t_name, t_sql in tables:
                print(f"\n  Table: {t_name}")
                print(f"  SQL: {t_sql}")
                cursor.execute(f"PRAGMA table_info('{t_name}');")
                cols = cursor.fetchall()
                for col in cols:
                    print(f"    - {col[1]} ({col[2]})")
                cursor.execute(f"SELECT count(*) FROM '{t_name}';")
                cnt = cursor.fetchone()[0]
                print(f"    Total Rows: {cnt}")
            conn.close()
        except Exception as e:
            print(f"  Error reading {db_file}: {e}")

if __name__ == "__main__":
    inspect_convos()
