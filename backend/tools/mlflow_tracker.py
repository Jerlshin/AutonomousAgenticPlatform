import json
from pathlib import Path
from typing import Any, Dict, List
import os

import mlflow
from core.config import settings
from core.models import ExperimentRecord, CodeArtifact


class ExperimentTracker:
    def __init__(self):
        self.tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
        mlflow.set_tracking_uri(self.tracking_uri)
        try:
            mlflow.set_experiment("Autonomous-AI-Platform")
        except Exception as e:
            print(f"Warning: Could not connect to MLFlow: {e}")

    def log_run(
        self, 
        task_id: str, 
        status: str,
        metrics: Dict[str, float] = None, 
        params: Dict[str, Any] = None, 
        code_artifact: CodeArtifact | None = None,
    ) -> ExperimentRecord:
        try:
            with mlflow.start_run(run_name=task_id) as run:
                if metrics:
                    mlflow.log_metrics(metrics)
                if params:
                    mlflow.log_params(params)
                
                artifacts_list = []
                if code_artifact:
                    import tempfile
                    from pathlib import Path
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_path = Path(tmpdir) / code_artifact.filename
                        tmp_path.write_text(code_artifact.content, encoding="utf-8")
                        mlflow.log_artifact(str(tmp_path))
                        artifacts_list.append(code_artifact.filename)
                        
                mlflow.set_tag("status", status)
                
                return ExperimentRecord(
                    task_id=task_id,
                    status=status,
                    metrics=metrics or {},
                    params=params or {},
                    artifacts=artifacts_list,
                    run_id=run.info.run_id,
                )
        except Exception as e:
            print(f"Warning: Failed to log run to MLFlow: {e}")
            return ExperimentRecord(
                task_id=task_id,
                status=status,
                metrics=metrics or {},
                params=params or {},
                artifacts=[]
            )

    def list_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        try:
            experiment = mlflow.get_experiment_by_name("Autonomous-AI-Platform")
            if not experiment:
                return []
                
            runs = mlflow.search_runs(
                experiment_ids=[experiment.experiment_id],
                max_results=limit,
                order_by=["start_time DESC"]
            )
            
            # Convert pandas dataframe to list of dicts safely
            if runs.empty:
                return []
            
            # Convert NaN to None for JSON serialization
            runs = runs.replace({float('nan'): None})
            return runs.to_dict('records')
        except Exception as e:
            print(f"Warning: Failed to fetch runs from MLFlow: {e}")
            return []

experiment_tracker = ExperimentTracker()
