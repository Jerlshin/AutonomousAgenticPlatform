import asyncio
import websockets
import json

async def run_ws():
    uri = "ws://localhost:8000/workflows/ws"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "user_request": "Write a python script to retrieve the latest news on Indian politics and perform sentiment analysis and compare various ML models",
            "max_retries": 1
        }))
        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)
                print(f"Node: {data.get('node')}")
            except websockets.exceptions.ConnectionClosed as e:
                print(f"Connection closed: {e}")
                break

if __name__ == "__main__":
    asyncio.run(run_ws())
