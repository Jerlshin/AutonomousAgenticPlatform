import json

from fastapi import FastAPI, WebSocket, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from api.schemas import HealthResponse, WorkflowRunRequest
from core.workflow_runner import run_workflow, stream_workflow
from tools.mlflow_tracker import experiment_tracker


app = FastAPI(
    title="Autonomous Multi-Agent AI R&D Platform",
    version="0.1.0",
    description="Local Ollama-powered multi-agent backend for research, coding, execution, debugging, and evaluation.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="multi-agent-backend")


@app.post("/workflows/run")
async def run_workflow_endpoint(payload: WorkflowRunRequest):
    state = await run_workflow(payload.user_request, max_retries=payload.max_retries)
    return jsonable_encoder(state)


@app.post("/workflows/stream")
async def stream_workflow_endpoint(request: Request, payload: WorkflowRunRequest):
    async def event_stream():
        async for update in stream_workflow(payload.user_request, max_retries=payload.max_retries):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(jsonable_encoder(update))}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.websocket("/workflows/ws")
async def workflow_websocket(websocket: WebSocket):
    await websocket.accept()
    payload = await websocket.receive_json()
    request = WorkflowRunRequest(**payload)
    async for update in stream_workflow(request.user_request, max_retries=request.max_retries):
        await websocket.send_json(jsonable_encoder(update))
    await websocket.close()


@app.get("/experiments")
def list_experiments(limit: int = 20):
    return {"runs": experiment_tracker.list_runs(limit=limit)}
