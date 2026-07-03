import sqlite3
import json
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = sqlite3.connect('data/tenders.db')
conn.row_factory = sqlite3.Row

# Latest tender
row = conn.execute("SELECT * FROM tenders ORDER BY id DESC LIMIT 1").fetchone()
r = dict(row)
tender_id = r['id']
print(f"=== Tender id={tender_id} ===")
print(f"  total_estimate : {r['total_estimate']}")
print(f"  total_positions: {r['total_positions']}")
print(f"  matched_pos    : {r['matched_positions']}")
print(f"  verdict        : {r['verdict']}")

positions_raw = r.get('positions')
if positions_raw:
    try:
        positions = json.loads(positions_raw)
        print(f"  positions JSON : {len(positions)} items")
        if positions:
            print(f"  first item     : {positions[0]}")
    except Exception as e:
        print(f"  positions JSON parse error: {e}")
        print(f"  raw (first 300): {positions_raw[:300]}")
else:
    print("  positions      : NULL / empty")

print()
docs = conn.execute(f"SELECT * FROM documents WHERE tender_id={tender_id}").fetchall()
print(f"Documents ({len(docs)}):")
for d in docs:
    dd = dict(d)
    print(f"  {dd['filename']} | status={dd['status']} | error={dd['error_msg']}")
