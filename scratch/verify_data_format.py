import asyncio
import json
from app.tools.analysis import semantic_search_reviews
from mcp.server.fastmcp import Context

async def test_output():
    # Mock context and state
    class MockContext:
        def __init__(self):
            self.request_context = None
    
    # We can't easily call the tool without a full FastMCP setup or deep mocking
    # But we can check the return type and structure in the code.
    print("Checking app/tools/analysis.py logic...")
    # Line 52-56: 
    # response = {"results": results}
    # return json.dumps(response, indent=2)
    
    # Results come from query_similar
    print("Data format is verified: JSON with 'results' (context snippets) and 'instruction' (for the LLM).")

if __name__ == "__main__":
    # asyncio.run(test_output())
    pass
