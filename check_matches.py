import sqlite3, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from margin_calculator import calculate_margin

conn = sqlite3.connect('data/tenders.db')
conn.row_factory = sqlite3.Row

t = conn.execute('SELECT id, positions, total_positions, matched_positions, total_estimate FROM tenders ORDER BY id DESC LIMIT 1').fetchone()
print(f"Tender id={t['id']} | total_positions={t['total_positions']} | matched_positions={t['matched_positions']} | total_estimate={t['total_estimate']}")

positions = json.loads(t['positions']) if t['positions'] else []
price_rows = conn.execute('SELECT name, unit, my_price, category FROM price_items').fetchall()
price_items = [dict(r) for r in price_rows]

print(f"price_items count: {len(price_items)}")
print(f"smeta positions : {len(positions)}")

margin = calculate_margin(positions, price_items)
print(f"\nRecalculated match: {margin['matched_count']} / {margin['total_count']}")
print(f"margin_byn : {margin['margin_byn']}")
print(f"margin_pct : {margin['margin_pct']}%")

print("\nFirst 5 matched positions:")
for p in margin['positions'][:5]:
    print(f"  {p['name'][:60]} | tender={p['tender_price']} | mine={p['my_price']} | diff={p['diff_pct']}%")
