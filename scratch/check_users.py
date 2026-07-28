import sqlite3
import os

db_path = r'C:\Users\PaarthGala\Coding\github-pr-context-mcp\chroma_db\usage_stats.db'

if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check table names first
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print(f"Tables: {tables}")
    
    for table in tables:
        table_name = table[0]
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        print(f"Table {table_name} has {count} rows")
        
        # Try to find unique users if it's a usage or pings table
        if 'usage' in table_name.lower() or 'pings' in table_name.lower():
            try:
                # Check column names
                cursor.execute(f"PRAGMA table_info({table_name})")
                cols = [col[1] for col in cursor.fetchall()]
                print(f"Columns in {table_name}: {cols}")
                
                user_col = next((c for c in cols if 'user' in c.lower() or 'id' in c.lower()), None)
                if user_col:
                    cursor.execute(f"SELECT COUNT(DISTINCT {user_col}) FROM {table_name}")
                    unique_users = cursor.fetchone()[0]
                    print(f"Unique users in {table_name} (based on {user_col}): {unique_users}")
            except Exception as e:
                print(f"Error querying {table_name}: {e}")
    
    conn.close()
