from langgraph.graph import StateGraph, END

from agents.coder import CodingAgent
from agents.debugger import DebugAgent
from agents.evaluator import EvaluationAgent
from agents.executor import ExecutionAgent
from agents.mlops import MLOpsAgent
from core.state import AgentState
from agents.planner import PlannerAgent
from agents.researcher import ResearchAgent

planner_agent = PlannerAgent()
research_agent = ResearchAgent()
coder_agent = CodingAgent()
execution_agent = ExecutionAgent()
debug_agent = DebugAgent()
mlops_agent = MLOpsAgent()
evaluation_agent = EvaluationAgent()

workflow = StateGraph(AgentState)

def planner_node(state: AgentState):
    return planner_agent.run(state)

def research_node(state: AgentState):
    return research_agent.run(state)

def coding_node(state: AgentState):
    return coder_agent.run(state)

def execution_node(state: AgentState):
    return execution_agent.run(state)

def debug_node(state: AgentState):
    return debug_agent.run(state)

def mlops_node(state: AgentState):
    return mlops_agent.run(state)

def evaluation_node(state: AgentState):
    return evaluation_agent.run(state)

def route_after_execution(state: AgentState) -> str:
    result = state.get("latest_execution")
    if result and result.success:
        return "mlops"
    if state.get("retry_count", 0) < state.get("max_retries", 2):
        return "debugger"
    return "mlops"

workflow.add_node("planner", planner_node)
workflow.add_node("researcher", research_node)
workflow.add_node("coder", coding_node)
workflow.add_node("executor", execution_node)
workflow.add_node("debugger", debug_node)
workflow.add_node("mlops", mlops_node)
workflow.add_node("evaluator", evaluation_node)

# Entry
workflow.set_entry_point("planner")

# Edges
workflow.add_edge("planner", "researcher")
workflow.add_edge("researcher", "coder")
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

app = workflow.compile()
