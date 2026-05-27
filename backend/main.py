import argparse

from core.workflow_runner import stream_workflow


def main():
    parser = argparse.ArgumentParser(description="Run the autonomous multi-agent backend workflow.")
    parser.add_argument(
        "request",
        nargs="?",
        default="Build a Python script that calculates the average closing price from a small in-memory AAPL price list.",
        help="User request for the agent system.",
    )
    parser.add_argument("--max-retries", type=int, default=None)
    args = parser.parse_args()

    final_state = None
    for update in stream_workflow(args.request, max_retries=args.max_retries):
        node = update["node"]
        state = update["state"]
        final_state = state
        print(f"\n=== Completed Node: {node} ===")
        print(f"Status: {state['status']}")
        if state["events"]:
            print(f"Latest Event: {state['events'][-1].event_type}")

    print("\n================ FINAL ARTIFACT ================\n")
    if final_state and final_state["generated_artifacts"]:
        artifact = final_state["generated_artifacts"][-1]
        print(f"Filename: {artifact.filename}\n")
        print(artifact.content)

    if final_state and final_state.get("evaluation"):
        print("\n================ EVALUATION ================\n")
        print(final_state["evaluation"].summary)


if __name__ == "__main__":
    main()
