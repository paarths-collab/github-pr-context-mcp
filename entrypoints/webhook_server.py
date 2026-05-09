"""
Standalone GitHub Webhook Server for Incremental Indexing.
Run this on a publicly accessible server (or via ngrok) to listen for GitHub 'pull_request' events.
When a PR is closed and merged, it will automatically fetch and index the new PR into the permanent storage.
"""
import json
import hmac
import hashlib
import os
import sys
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import existing indexing logic
from fetcher.client import fetch_prs
from storage.vector_store import index_prs

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

def verify_signature(payload_body, secret_token, signature_header):
    """Verify that the payload was sent from GitHub by validating SHA256."""
    if not secret_token:
        return True # Bypass if no secret configured
    if not signature_header:
        return False
    
    hash_object = hmac.new(secret_token.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected_signature, signature_header)

def async_index_single_pr(repo_full_name, pr_number):
    """Fetch and index a single PR in a background thread."""
    async def _run():
        print(f"[*] Fetching newly merged PR #{pr_number} for {repo_full_name}...")
        owner, name = repo_full_name.split("/", 1)
        # Fetch with pages=1, looking specifically for this PR and newer
        prs = await fetch_prs(owner, name, pages=1, github_token=GITHUB_TOKEN, since_pr_number=pr_number-1)
        
        # Filter down to just this PR
        target_pr = [p for p in prs if p["number"] == pr_number]
        if not target_pr:
            print(f"[-] Could not fetch PR #{pr_number} (maybe API latency or it wasn't returned)")
            return
            
        print(f"[*] Indexing PR #{pr_number} into permanent storage...")
        # A webhook delivers one PR, not a complete newest-first sweep, so the
        # refresh watermark must not move. Advancing it here would let the next
        # ensure_repo_ready(refresh=True) skip anything updated before this PR
        # that had not been indexed yet.
        count = index_prs(repo_full_name, target_pr, temporary=False, advance_watermark=False)
        print(f"[+] Incremental index complete. {count} new documents added.")
        
    asyncio.run(_run())

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        signature = self.headers.get('X-Hub-Signature-256')
        
        if not verify_signature(post_data, WEBHOOK_SECRET, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden: Invalid signature")
            return
            
        event_type = self.headers.get('X-GitHub-Event')
        if event_type == "ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Pong!")
            return
            
        if event_type == "pull_request":
            try:
                payload = json.loads(post_data)
                action = payload.get("action")
                
                # We only care when a PR is closed and merged
                if action == "closed" and payload.get("pull_request", {}).get("merged"):
                    pr_number = payload["pull_request"]["number"]
                    repo_full_name = payload["repository"]["full_name"]
                    print(f"[Webhook] Received merged PR #{pr_number} for {repo_full_name}")
                    
                    # Kick off indexing in background to avoid blocking GitHub timeout
                    threading.Thread(target=async_index_single_pr, args=(repo_full_name, pr_number), daemon=True).start()
            except json.JSONDecodeError:
                pass
                
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run(port=8080):
    server_address = ('', port)
    httpd = HTTPServer(server_address, WebhookHandler)
    print(f"Starting GitHub Webhook listener on port {port}...")
    if not WEBHOOK_SECRET:
        print("WARNING: GITHUB_WEBHOOK_SECRET is not set. Signature verification is disabled.")
    httpd.serve_forever()

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8080))
    run(port)
