import sys
import os
import asyncio
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.getcwd())

from app.mcp_app import mcp
from mcp.server.fastmcp import Context

async def validate_tool(tool_name, **kwargs):
    print(f"Testing tool: {tool_name}...", end=" ")
    try:
        # Mock Context - some tools might use it via mcp.get_context()
        mock_ctx = MagicMock(spec=Context)
        mock_ctx.session = MagicMock()
        mock_ctx.client_id = "test-client"
        
        # We'll use the tool functions directly because call_tool might try to do things
        # like JSON serialization or session management that are hard to mock here.
        # But wait, we want to check for IMPORT errors and LOGIC errors.
        
        found_tool = None
        # Access the tool function via the tool manager
        for t in mcp._tool_manager.list_tools():
            if t.name == tool_name:
                found_tool = t
                break
        
        if not found_tool:
            print(f"FAILED (Tool not found in FastMCP)")
            return False

        fn = found_tool.fn
        
        # Add ctx to kwargs if expected
        import inspect
        sig = inspect.signature(fn)
        if "ctx" in sig.parameters:
            kwargs["ctx"] = mock_ctx
        
        # Call the tool (might be async or sync)
        if inspect.iscoroutinefunction(fn):
            result = await fn(**kwargs)
        else:
            result = fn(**kwargs)
            
        print(f"SUCCESS (Result type: {type(result).__name__})")
        return True
    except Exception as e:
        print(f"FAILED (Error: {e})")
        # import traceback
        # traceback.print_exc()
        return False

# Setup dummy environment
os.environ["GITHUB_TOKEN"] = "test-token"

tools_to_test = [
    ("list_indexed_repos", {}),
    ("ensure_repo_ready", {"repo": "owner/repo"}),
    ("get_index_stats", {"repo": "owner/repo"}),
    ("set_active_repo", {"repo": "owner/repo"}),
    ("delete_repo_index", {"repo": "owner/repo"}),
    ("update_settings", {"github_token": "new-token"}),
    ("get_usage_stats", {"days": 7}),
    ("semantic_search_reviews", {"query": "test query"}),
    ("get_team_review_patterns", {}),
    ("review_code_with_history", {"code": "print('hello')"}),
    ("generate_code_from_history", {"task": "write a function"}),
    ("generate_repo_rules", {}),
]

async def run_all():
    print("=== STARTING COMPREHENSIVE TOOL VALIDATION ===\n")
    all_passed = True
    for name, args in tools_to_test:
        if not await validate_tool(name, **args):
            all_passed = False

    if all_passed:
        print("\n=== ALL TOOLS VALIDATED SUCCESSFULLY ===")
        sys.exit(0)
    else:
        print("\n=== SOME TOOLS FAILED VALIDATION ===")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_all())
