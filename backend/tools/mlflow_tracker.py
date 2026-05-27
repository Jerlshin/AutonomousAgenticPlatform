import json
from pathlib import Path
from typing import Any, Dict, List

from core.config import settings
from core.models import ExperimentRecord


class ExperimentTracker:
    def __init__(self, log_path: str = settings.experiment_log_path):
        self.log_path = Path(log_path)

    def log_run(
        self,
        task_id: str,
        status: str,
        metrics: Dict[str, float],
        params: Dict[str, Any],
        artifacts: List[str],
    ) -> ExperimentRecord:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = ExperimentRecord(
            task_id=task_id,
            status=status,
            metrics=metrics,
            params=params,
            artifacts=artifacts,
        )
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(record.json() + "\n")
        return record

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]


experiment_tracker = ExperimentTracker()
