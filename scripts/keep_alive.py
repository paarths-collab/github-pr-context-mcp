import time
import requests
from datetime import datetime

# The URL to keep alive
TARGET_URL = "https://mcp-quant-brain.onrender.com/healthz"

def ping():
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        response = requests.get(TARGET_URL, timeout=10)
        if response.status_code == 200:
            print(f"[{now}] ✅ Ping successful: {TARGET_URL}")
        else:
            print(f"[{now}] ⚠️ Ping returned status {response.status_code}")
    except Exception as e:
        print(f"[{now}] ❌ Ping failed: {e}")

def main():
    print(f"Starting keep-alive service for {TARGET_URL}...")
    print("Will ping every 60 seconds. Press Ctrl+C to stop.")
    
    while True:
        ping()
        time.sleep(60)

if __name__ == "__main__":
    main()
