import asyncio
import os
import sys

# Add backend dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.workflow_runner import stream_workflow

async def test_run():
    print("Starting test run...")
    try:
        async for update in stream_workflow("Write a simple hello world script in Python.", max_retries=1):
            print(f"Node: {update.get('node')}")
            state = update.get('state', {})
            print(f"Status: {state.get('status')}")
    except Exception as e:
        print(f"Error during execution: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_run())
