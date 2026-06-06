import { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { Clock, Play, ShieldAlert, CheckCircle, XCircle } from 'lucide-react';
import type { AgentState } from '../types';

interface AgentProgressProps {
  state: AgentState | null;
  currentNode: string | null;
}

export const AgentProgress = ({ state, currentNode }: AgentProgressProps) => {
  const [elapsedTime, setElapsedTime] = useState(0);

  useEffect(() => {
    let interval: NodeJS.Timeout;
    
    if (state && !['completed', 'failed', 'error'].includes(state.status.toLowerCase()) && currentNode !== 'Finished' && currentNode !== 'Terminated') {
      interval = setInterval(() => setElapsedTime(prev => prev + 1), 1000);
    } else if (!state) {
      setElapsedTime(0);
    }

    return () => clearInterval(interval);
  }, [state?.status, currentNode]);

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  if (!state) return null;

  const getStatusClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'completed': return 'completed';
      case 'failed': return 'failed';
      case 'running': return 'running';
      case 'waiting_for_input': return 'running'; // Keep it glowing blue when waiting
      default: return 'initialized';
    }
  };

  const isRunning = !['completed', 'failed'].includes(state.status.toLowerCase()) && currentNode !== 'Finished' && currentNode !== 'Terminated';

  return (
    <motion.div 
      initial={{ opacity: 0, x: 20 }}
      animate={{ opacity: 1, x: 0 }}
      className="glass-panel" 
      style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ fontSize: '1.1rem', fontWeight: 600 }}>Workflow Status</h3>
        <span className={`status-badge ${getStatusClass(state.status)}`}>
          {state.status.replace(/_/g, ' ')}
        </span>
      </div>
      
      <div style={{ 
        display: 'flex', gap: '0.75rem', alignItems: 'center', 
        background: 'rgba(0,0,0,0.3)', padding: '1rem', borderRadius: '8px',
        border: '1px solid rgba(255,255,255,0.05)'
      }}>
        {isRunning ? (
          <motion.div animate={{ rotate: 360 }} transition={{ duration: 2, repeat: Infinity, ease: 'linear' }}>
            <Play size={18} color="var(--accent-blue)" />
          </motion.div>
        ) : state.status.toLowerCase() === 'completed' ? (
          <CheckCircle size={18} color="var(--accent-green)" />
        ) : (
          <XCircle size={18} color="var(--accent-red)" />
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
          <span style={{ color: 'var(--text-secondary)', fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Current Node</span>
          <span style={{ color: isRunning ? 'var(--accent-blue)' : '#fff', fontWeight: 600, fontSize: '1rem' }}>
            {currentNode || 'Idle'}
          </span>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem', fontSize: '0.9rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Clock size={14} /> Time Elapsed
          </span>
          <span style={{ fontFamily: 'Fira Code, monospace', fontWeight: 500 }}>{formatTime(elapsedTime)}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Graph Iterations</span>
          <span style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 8px', borderRadius: '12px' }}>{state.iterations}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Error Retries</span>
          <span style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 8px', borderRadius: '12px' }}>{state.retry_count} / {state.max_retries}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: 'var(--text-secondary)' }}>Generated Artifacts</span>
          <span style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 8px', borderRadius: '12px' }}>{state.generated_artifacts.length}</span>
        </div>
      </div>
    </motion.div>
  );
};
