import asyncio
import os
import sys

# Add backend dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.workflow_runner import stream_workflow

async def test_run():
    print("Starting test run...")
    input_queue = asyncio.Queue()
    
    # We simulate a user input after 5 seconds
    async def simulate_user():
        await asyncio.sleep(5)
        print("Simulating user input...")
        await input_queue.put("NO")
        
    asyncio.create_task(simulate_user())
    
    try:
        async for update in stream_workflow("Write a simple hello world script in Python.", max_retries=1, input_queue=input_queue):
            print(f"Node: {update.get('node')}")
            state = update.get('state', {})
            print(f"Status: {state.get('status')}")
    except Exception as e:
        print(f"Error during execution: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_run())
