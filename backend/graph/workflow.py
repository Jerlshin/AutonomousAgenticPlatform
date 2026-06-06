from langgraph.graph import StateGraph, END

from agents.coder import CodingAgent
from agents.debugger import DebugAgent
from agents.evaluator import EvaluationAgent
from agents.executor import ExecutionAgent
from agents.mlops import MLOpsAgent
from core.state import AgentState
from agents.planner import PlannerAgent
from agents.researcher import ResearchAgent
from core.enums import WorkflowStatus
from langchain_core.runnables import RunnableConfig

planner_agent = PlannerAgent()
research_agent = ResearchAgent()
coder_agent = CodingAgent()
execution_agent = ExecutionAgent()
debug_agent = DebugAgent()
mlops_agent = MLOpsAgent()
evaluation_agent = EvaluationAgent()

workflow = StateGraph(AgentState)

async def planner_node(state: AgentState):
    return await planner_agent.run(state)

async def research_node(state: AgentState):
    return await research_agent.run(state)

async def coding_node(state: AgentState):
    return await coder_agent.run(state)

async def execution_node(state: AgentState):
    return await execution_agent.run(state)

async def debug_node(state: AgentState):
    return await debug_agent.run(state)

async def mlops_node(state: AgentState):
    return await mlops_agent.run(state)

async def evaluation_node(state: AgentState):
    return await evaluation_agent.run(state)

async def human_intervention_node(state: AgentState, config: RunnableConfig):
    input_queue = config.get("configurable", {}).get("input_queue")
    if input_queue:
        human_response = await input_queue.get()
    else:
        human_response = "NO" # Fallback if no queue
        
    return {
        "human_response": human_response,
        "human_query": None, # Clear query
        "status": WorkflowStatus.RESEARCHING, # Back to researching flow
        # Re-inject the response into context
        "retrieved_context": state.get("retrieved_context", []) + [f"Human Input/API Key: {human_response}"]
    }

def route_after_researcher(state: AgentState) -> str:
    if state.get("human_query"):
        return "human_intervention"
    return "coder"

def route_after_execution(state: AgentState) -> str:
    result = state.get("latest_execution")
    if result and result.success:
        return "mlops"
    if state.get("retry_count", 0) < state.get("max_retries", 2):
        return "debugger"
    return "mlops"

workflow.add_node("planner", planner_node)
workflow.add_node("researcher", research_node)
workflow.add_node("human_intervention", human_intervention_node)
workflow.add_node("coder", coding_node)
workflow.add_node("executor", execution_node)
workflow.add_node("debugger", debug_node)
workflow.add_node("mlops", mlops_node)
workflow.add_node("evaluator", evaluation_node)

# Entry
workflow.set_entry_point("planner")

# Edges
workflow.add_edge("planner", "researcher")
workflow.add_conditional_edges(
    "researcher",
    route_after_researcher,
    {
        "human_intervention": "human_intervention",
        "coder": "coder",
    }
)
workflow.add_edge("human_intervention", "coder")
workflow.add_edge("coder", "executor")
workflow.add_conditional_edges(
    "executor",
    route_after_execution,
    {
        "debugger": "debugger",
        "mlops": "mlops",
    },
)
workflow.add_edge("debugger", "executor")
workflow.add_edge("mlops", "evaluator")
workflow.add_edge("evaluator", END)
