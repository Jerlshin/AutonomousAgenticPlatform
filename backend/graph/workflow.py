from langgraph.graph import StateGraph, END
from core.state import AgentState
from agents.planner import PlannerAgent
from agents.coder import CodingAgent

planner_agent = PlannerAgent()
coder_agent = CodingAgent()

workflow = StateGraph(AgentState)

def planner_node(state: AgentState):
    return planner_agent.run(state)

def coding_node(state: AgentState):
    return coder_agent.run(state)

workflow.add_node(planner_node, name="planner")
workflow.add_node(coding_node, name="coder")

# Entry
workflow.set_entry_point("planner")

# Edges
workflow.add_edge("planner", "coder")
workflow.add_edge("coder", END)

app = workflow.compile()
