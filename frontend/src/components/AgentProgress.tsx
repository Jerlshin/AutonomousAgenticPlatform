import type { AgentState } from '../types';

interface AgentProgressProps {
  state: AgentState | null;
  currentNode: string | null;
}

export const AgentProgress = ({ state, currentNode }: AgentProgressProps) => {
  if (!state) return null;

  const getStatusClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'completed': return 'completed';
      case 'failed': return 'failed';
      case 'running': return 'running';
      default: return 'initialized';
    }
  };

  return (
    <div className="glass-panel" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ fontSize: '1.1rem', fontWeight: 600 }}>Workflow Status</h3>
        <span className={`status-badge ${getStatusClass(state.status)}`}>
          {state.status}
        </span>
      </div>
      
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', fontSize: '0.9rem' }}>
        <span style={{ color: 'var(--text-secondary)' }}>Current Node:</span>
        <span style={{ 
          background: 'rgba(255,255,255,0.1)', 
          padding: '2px 8px', 
          borderRadius: '4px',
          color: 'var(--accent-blue)',
          fontWeight: 500
        }}>
          {currentNode || 'Idle'}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', fontSize: '0.85rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Iterations</span>
          <span>{state.iterations}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Retries</span>
          <span>{state.retry_count} / {state.max_retries}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Artifacts</span>
          <span>{state.generated_artifacts.length}</span>
        </div>
      </div>
    </div>
  );
};
