export interface PlanStep {
  step_id: string;
  title: string;
  description: string;
  dependencies: string[];
  completed: boolean;
}

export interface CodeArtifact {
  artifact_id: string;
  filename: string;
  language: string;
  content: string;
  created_at: string;
}

export interface AgentEvent {
  event_id: string;
  event_type: string;
  source_agent: string;
  timestamp: string;
  payload: Record<string, any>;
}

export interface ExecutionLog {
  timestamp: string;
  level: string;
  message: string;
}

export interface ExecutionResult {
  success: boolean;
  stdout: string | null;
  stderr: string | null;
  exit_code: number | null;
  execution_time: number | null;
  command: string | null;
  artifact_id: string | null;
}

export interface EvaluationReport {
  passed: boolean;
  score: number;
  summary: string;
  checks: Record<string, any>;
}

export interface AgentState {
  task_id: string;
  user_request: string;
  current_plan: PlanStep[];
  active_step_id: string | null;
  generated_artifacts: CodeArtifact[];
  latest_execution: ExecutionResult | null;
  execution_logs: ExecutionLog[];
  retrieved_context: string[];
  reflection_notes: string[];
  events: AgentEvent[];
  retry_count: number;
  iterations: number;
  status: string;
  metrics: Record<string, number>;
  evaluation: EvaluationReport | null;
  errors: string[];
  max_retries: number;
}
