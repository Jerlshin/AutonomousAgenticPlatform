#!/bin/bash

# Initialize conda so we can use activate (if necessary, though Makefile uses conda run)
# eval "$(conda shell.bash hook)"
# conda activate ai_env

echo "=========================================="
echo "Starting Autonomous AI Platform"
echo "=========================================="

echo "[1/3] Starting database infrastructure..."
make infra-up

echo "[2/3] Starting backend (FastAPI)..."
make backend-run &
BACKEND_PID=$!

echo "[3/3] Starting frontend (Vite)..."
cd frontend
npm run dev &
FRONTEND_PID=$!

cd ..

# Trap Ctrl+C to clean up background processes and infrastructure
cleanup() {
    echo ""
    echo "=========================================="
    echo "Stopping services..."
    echo "=========================================="
    kill $FRONTEND_PID
    kill $BACKEND_PID
    make infra-down
    echo "Done."
    exit 0
}

trap cleanup SIGINT SIGTERM

echo ""
echo "=========================================="
echo "Application is running."
echo "- Backend: http://localhost:8000"
echo "- Frontend: Check Vite output above (usually http://localhost:5173)"
echo "Press Ctrl+C to stop all services."
echo "=========================================="

# Wait indefinitely for user to press Ctrl+C
wait
