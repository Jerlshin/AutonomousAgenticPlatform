import asyncio
from agents.researcher import ResearchAgent
from tools.research import research_tool

async def main():
    agent = ResearchAgent()
    state = {
        "user_request": "Write a python script to retrieve the latest news on Indian politics and perform sentiment analysis and compare various ML models",
        "current_plan": []
    }
    
    print("Testing research tool...")
    hits = await research_tool.search(state["user_request"], limit=5)
    print("Hits:", hits)
    
    print("Running research agent...")
    res = await agent.run(state)
    print("Result:", res)

if __name__ == "__main__":
    asyncio.run(main())
