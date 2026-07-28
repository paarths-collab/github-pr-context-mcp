import sys
import os

# Add project root to sys.path
sys.path.append(os.getcwd())

from app.mcp_app import mcp

print("=== REGISTERED TOOLS ===")
for tool_name in mcp._tools.keys():
    print(f"- {tool_name}")

print("\n=== REGISTERED ROUTES ===")
for route_path in mcp._routes.keys():
    print(f"- {route_path}")
