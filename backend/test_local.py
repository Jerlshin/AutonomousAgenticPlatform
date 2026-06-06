import asyncio
from core.workflow_runner import stream_workflow

async def test():
    try:
        async for update in stream_workflow("Write a python script to retrieve the latest news on Indian politics", max_retries=1):
            print("update:", update['node'])
            if 'state' in update:
                print("status:", update['state'].get('status'))
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(test())
