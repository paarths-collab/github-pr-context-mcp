import os
import sys
import time
import threading
import requests
from app.mcp_app import mcp

def _run_keep_alive():
    url = os.getenv("KEEP_ALIVE_URL")
    if not url:
        return
        
    url = f"{url.rstrip('/')}/healthz"
    print(f"Keep-alive service started. Pinging {url} every 60s.", file=sys.stderr)
    
    # Wait for server to boot
    time.sleep(10)
    
    while True:
        try:
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(60)

def main() -> None:
    if os.getenv("KEEP_ALIVE_URL"):
        threading.Thread(target=_run_keep_alive, daemon=True).start()
    
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
