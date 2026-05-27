import { useState, useRef, useEffect } from 'react';
import type { AgentState } from '../types';

interface StateVisualizerProps {
  state: AgentState | null;
}

export const StateVisualizer = ({ state }: StateVisualizerProps) => {
  const [activeTab, setActiveTab] = useState<'plan' | 'code' | 'logs' | 'evaluation' | 'updates'>('updates');
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  const logsEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (activeTab === 'logs') {
      logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [state?.execution_logs, activeTab]);

  if (!state) {
    return (
      <div className="glass-panel" style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div className="empty-state">
          <p>Submit a task to see real-time visualization.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="glass-panel" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: '1.5rem' }}>
      <div className="tabs-header">
        <button 
          className={`tab-btn ${activeTab === 'updates' ? 'active' : ''}`}
          onClick={() => setActiveTab('updates')}
        >
          Live Updates
        </button>
        <button 
          className={`tab-btn ${activeTab === 'plan' ? 'active' : ''}`}
          onClick={() => setActiveTab('plan')}
        >
          Plan ({state.current_plan?.length || 0})
        </button>
        <button 
          className={`tab-btn ${activeTab === 'code' ? 'active' : ''}`}
          onClick={() => setActiveTab('code')}
        >
          Code Artifacts ({state.generated_artifacts?.length || 0})
        </button>
        <button 
          className={`tab-btn ${activeTab === 'logs' ? 'active' : ''}`}
          onClick={() => setActiveTab('logs')}
        >
          Execution Logs ({state.execution_logs?.length || 0})
        </button>
        <button 
          className={`tab-btn ${activeTab === 'evaluation' ? 'active' : ''}`}
          onClick={() => setActiveTab('evaluation')}
        >
          Evaluation
        </button>
      </div>

      <div className="content-area animate-fade-in">
        {activeTab === 'updates' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {state.events?.map((ev) => (
              <div key={ev.event_id} style={{
                background: 'rgba(255,255,255,0.03)',
                borderLeft: '3px solid var(--accent-blue)',
                padding: '1rem',
                borderRadius: '0 8px 8px 0'
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                  <strong style={{ color: 'var(--accent-blue)', textTransform: 'capitalize' }}>
                    {ev.source_agent.replace('_', ' ')}
                  </strong>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                    {new Date(ev.timestamp).toLocaleTimeString()}
                  </span>
                </div>
                <div style={{ fontSize: '0.9rem', color: '#e2e8f0' }}>
                  {ev.event_type.replace('_', ' ')}: {JSON.stringify(ev.payload)}
                </div>
              </div>
            ))}
            {(!state.events || state.events.length === 0) && (
              <div className="empty-state">No agent updates yet...</div>
            )}
          </div>
        )}

        {activeTab === 'plan' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {state.current_plan?.map((step, idx) => (
              <div key={step.step_id} style={{
                background: step.completed ? 'rgba(16, 185, 129, 0.1)' : 'rgba(255,255,255,0.05)',
                border: `1px solid ${step.completed ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255,255,255,0.1)'}`,
                padding: '1rem',
                borderRadius: '8px',
                cursor: 'pointer'
              }} onClick={() => setExpandedSteps(prev => ({ ...prev, [step.step_id]: !prev[step.step_id] }))}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <h4 style={{ color: step.completed ? '#34d399' : '#fff', margin: 0 }}>
                    {idx + 1}. {step.title}
                  </h4>
                  <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                    {expandedSteps[step.step_id] ? '▼ Hide' : '▶ Show Details'}
                  </span>
                </div>
                {expandedSteps[step.step_id] && (
                  <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginTop: '0.75rem', paddingTop: '0.75rem', borderTop: '1px solid rgba(255,255,255,0.1)' }}>
                    {step.description}
                  </p>
                )}
              </div>
            ))}
            {(!state.current_plan || state.current_plan.length === 0) && (
              <div className="empty-state">No plan generated yet.</div>
            )}
          </div>
        )}

        {activeTab === 'code' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {state.generated_artifacts?.map((artifact, idx) => (
              <div key={idx} style={{ background: '#1e1e1e', borderRadius: '8px', overflow: 'hidden' }}>
                <div style={{ background: '#2d2d2d', padding: '0.5rem 1rem', fontSize: '0.8rem', color: '#ccc', display: 'flex', justifyContent: 'space-between' }}>
                  <span>{artifact.filename}</span>
                  <span>{artifact.language}</span>
                </div>
                <pre className="code-font" style={{ padding: '1rem', margin: 0, overflowX: 'auto', overflowY: 'auto', maxHeight: '500px', fontSize: '0.85rem' }}>
                  <code>{artifact.content}</code>
                </pre>
              </div>
            ))}
            {(!state.generated_artifacts || state.generated_artifacts.length === 0) && (
              <div className="empty-state">No code artifacts generated yet.</div>
            )}
          </div>
        )}

        {activeTab === 'logs' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            {state.execution_logs?.map((log, idx) => (
              <div key={idx} className="code-font" style={{ 
                fontSize: '0.85rem',
                color: log.level === 'ERROR' ? '#f87171' : log.level === 'WARNING' ? '#fbbf24' : '#a0a0b0',
                borderBottom: '1px solid rgba(255,255,255,0.05)',
                paddingBottom: '0.5rem'
              }}>
                <span style={{ opacity: 0.5, marginRight: '1rem' }}>[{log.timestamp}]</span>
                <span>{log.message}</span>
              </div>
            ))}
            <div ref={logsEndRef} />
            {(!state.execution_logs || state.execution_logs.length === 0) && (
              <div className="empty-state">No execution logs yet.</div>
            )}
          </div>
        )}

        {activeTab === 'evaluation' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {state.evaluation ? (
              <div style={{
                background: state.evaluation.passed ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                border: `1px solid ${state.evaluation.passed ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
                padding: '1.5rem',
                borderRadius: '8px'
              }}>
                <h3 style={{ color: state.evaluation.passed ? '#34d399' : '#f87171', marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  Verdict: {state.evaluation.passed ? 'PASSED' : 'FAILED'}
                  <span style={{ background: 'rgba(0,0,0,0.3)', padding: '2px 8px', borderRadius: '12px', fontSize: '0.9rem' }}>
                    Score: {state.evaluation.score}/100
                  </span>
                </h3>
                <p style={{ color: '#fff', fontSize: '0.95rem', lineHeight: 1.6 }}>{state.evaluation.summary}</p>
              </div>
            ) : (
              <div className="empty-state">No evaluation available yet.</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
