1. Agent (The Decision Maker)

An agent is an autonomous system powered by a LLM that can reason, break down a high-level goal into individual steps, choose which actions to take,and inspect the results of those actions to adjust its behavior

* It has state, memory, system prompts, ability to evaluate whether it should continue, pause, or re-plan

2. Tool (The Interface for the Agent)

A tool is a wrapper around a capability that the Agent can choose to invoke. It is exposed to the LLM with a specific "name", a clean human-readable "description", and a "schema" defining its inputs and outputs.

