import sqlite3
import json

db_path = "./chroma_db/usage_stats.db"
try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT mode, COUNT(DISTINCT user_hash), SUM(count) FROM pings GROUP BY mode")
    rows = cursor.fetchall()
    
    results = []
    for row in rows:
        results.append({
            "mode": row[0],
            "unique_users": row[1],
            "total_pings": row[2]
        })
    
    print(json.dumps(results, indent=2))
    conn.close()
except Exception as e:
    print(f"Error: {e}")
