import uuid

from graph.workflow import app
from core.enums import WorkflowStatus


initial_state = {
    "task_id": str(uuid.uuid4()),
    "user_request": "Build a Python script that downloads historical AAPL stock data and calculates the average closing price.",
    "current_plan": [],
    "active_step_id": None,
    "generated_artifacts": [],
    "latest_execution": None,
    "execution_logs": [],
    "retrieved_context": [],
    "reflection_notes": [],
    "retry_count": 0,
    "iterations": 0,
    "status": WorkflowStatus.INITIALIZED,
    "events": [],
    "metrics": {},
}


final_output = None


for output in app.stream(initial_state):
    for node_name, node_result in output.items():
        print(f"\n=== Completed Node: {node_name} ===")

        if "status" in node_result:
            print(f"Status: {node_result['status']}")

        if "events" in node_result:
            latest_event = node_result["events"][-1]
            print(f"Latest Event: {latest_event.event_type}")

        final_output = node_result


print("\n================ FINAL ARTIFACT ================\n")

if final_output and final_output.get("generated_artifacts"):
    artifact = final_output["generated_artifacts"][-1]

    print(f"Filename: {artifact.filename}\n")
    print(artifact.content)