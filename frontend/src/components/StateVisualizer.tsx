import { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-json';
import type { AgentState } from '../types';

interface StateVisualizerProps {
  state: AgentState | null;
}

export const StateVisualizer = ({ state }: StateVisualizerProps) => {
  const [activeTab, setActiveTab] = useState<'updates' | 'plan' | 'code' | 'logs' | 'evaluation'>('updates');
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});
  const logsEndRef = useRef<HTMLDivElement>(null);
  const updatesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (activeTab === 'logs') {
      logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
    if (activeTab === 'updates') {
      updatesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [state?.execution_logs, state?.events, activeTab]);

  useEffect(() => {
    if (activeTab === 'code') {
      Prism.highlightAll();
    }
  }, [state?.generated_artifacts, activeTab]);

  if (!state) {
    return (
      <div className="glass-panel" style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div className="empty-state">
          <motion.div 
            animate={{ opacity: [0.5, 1, 0.5] }} 
            transition={{ duration: 2, repeat: Infinity }}
            style={{ fontSize: '3rem', marginBottom: '1rem', color: 'var(--accent-blue)' }}
          >
            ⚡
          </motion.div>
          <p>Submit a task to initialize the autonomous engine.</p>
        </div>
      </div>
    );
  }

  const tabVariants = {
    hidden: { opacity: 0, y: 10 },
    visible: { opacity: 1, y: 0, transition: { staggerChildren: 0.1 } }
  };

  const itemVariants = {
    hidden: { opacity: 0, y: 10 },
    visible: { opacity: 1, y: 0 }
  };

  return (
    <div className="glass-panel" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: '0' }}>
      <div className="tabs-header" style={{ padding: '1.5rem 1.5rem 0 1.5rem', marginBottom: 0, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
        <button 
          className={`tab-btn ${activeTab === 'updates' ? 'active' : ''}`}
          onClick={() => setActiveTab('updates')}
        >
          Live Feed
        </button>
        <button 
          className={`tab-btn ${activeTab === 'plan' ? 'active' : ''}`}
          onClick={() => setActiveTab('plan')}
        >
          Strategy ({state.current_plan?.length || 0})
        </button>
        <button 
          className={`tab-btn ${activeTab === 'code' ? 'active' : ''}`}
          onClick={() => setActiveTab('code')}
        >
          Artifacts ({state.generated_artifacts?.length || 0})
        </button>
        <button 
          className={`tab-btn ${activeTab === 'logs' ? 'active' : ''}`}
          onClick={() => setActiveTab('logs')}
        >
          Terminal
        </button>
        <button 
          className={`tab-btn ${activeTab === 'evaluation' ? 'active' : ''}`}
          onClick={() => setActiveTab('evaluation')}
        >
          Evaluation
        </button>
      </div>

      <div className="content-area" style={{ padding: '1.5rem', background: 'transparent', border: 'none', borderRadius: 0 }}>
        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            variants={tabVariants}
            initial="hidden"
            animate="visible"
            exit={{ opacity: 0 }}
            style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
          >
            {activeTab === 'updates' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                {state.events?.map((ev, i) => (
                  <motion.div variants={itemVariants} key={ev.event_id || i} style={{
                    background: 'rgba(255,255,255,0.02)',
                    borderLeft: '3px solid var(--accent-blue)',
                    padding: '1.25rem',
                    borderRadius: '0 12px 12px 0',
                    boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)'
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.75rem', alignItems: 'center' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                        <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--accent-blue)', boxShadow: '0 0 10px var(--accent-blue)' }} />
                        <strong style={{ color: 'var(--accent-blue)', textTransform: 'capitalize', letterSpacing: '0.02em' }}>
                          {ev.source_agent.replace('_', ' ')}
                        </strong>
                      </div>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', fontFamily: 'Fira Code, monospace' }}>
                        {new Date(ev.timestamp).toLocaleTimeString()}
                      </span>
                    </div>
                    <div style={{ fontSize: '0.95rem', color: '#e2e8f0', lineHeight: 1.6 }}>
                      <span style={{ fontWeight: 600, color: '#fff', marginRight: '0.5rem' }}>[{ev.event_type.replace('_', ' ')}]</span> 
                      {typeof ev.payload === 'object' ? JSON.stringify(ev.payload, null, 2) : ev.payload}
                    </div>
                  </motion.div>
                ))}
                <div ref={updatesEndRef} />
                {(!state.events || state.events.length === 0) && (
                  <div className="empty-state" style={{ height: '100%' }}>Awaiting telemetries...</div>
                )}
              </div>
            )}

            {activeTab === 'plan' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                {state.current_plan?.map((step, idx) => (
                  <motion.div variants={itemVariants} key={step.step_id} style={{
                    background: step.completed ? 'rgba(16, 185, 129, 0.05)' : 'rgba(255,255,255,0.03)',
                    border: `1px solid ${step.completed ? 'rgba(16, 185, 129, 0.2)' : 'rgba(255,255,255,0.05)'}`,
                    padding: '1.25rem',
                    borderRadius: '12px',
                    cursor: 'pointer',
                    transition: 'all 0.2s ease',
                    boxShadow: step.completed ? 'inset 0 0 20px rgba(16, 185, 129, 0.02)' : 'none'
                  }} onClick={() => setExpandedSteps(prev => ({ ...prev, [step.step_id]: !prev[step.step_id] }))}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <h4 style={{ color: step.completed ? '#34d399' : '#fff', margin: 0, display: 'flex', alignItems: 'center', gap: '0.75rem', fontSize: '1.05rem', fontWeight: 500 }}>
                        <span style={{ 
                          display: 'inline-flex', alignItems: 'center', justifyContent: 'center', 
                          width: '24px', height: '24px', borderRadius: '50%', 
                          background: step.completed ? 'rgba(16, 185, 129, 0.2)' : 'rgba(255,255,255,0.1)',
                          fontSize: '0.8rem'
                        }}>
                          {step.completed ? '✓' : idx + 1}
                        </span>
                        {step.title}
                      </h4>
                      <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                        {expandedSteps[step.step_id] ? 'Collapse' : 'Expand'}
                      </span>
                    </div>
                    <AnimatePresence>
                      {expandedSteps[step.step_id] && (
                        <motion.div 
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: 'auto' }}
                          exit={{ opacity: 0, height: 0 }}
                          style={{ overflow: 'hidden' }}
                        >
                          <p style={{ color: 'var(--text-secondary)', fontSize: '0.95rem', marginTop: '1rem', paddingTop: '1rem', borderTop: '1px solid rgba(255,255,255,0.05)', lineHeight: 1.6 }}>
                            {step.description}
                          </p>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </motion.div>
                ))}
                {(!state.current_plan || state.current_plan.length === 0) && (
                  <div className="empty-state" style={{ height: '100%' }}>No strategy formulated.</div>
                )}
              </div>
            )}

            {activeTab === 'code' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
                {state.generated_artifacts?.map((artifact, idx) => (
                  <motion.div variants={itemVariants} key={idx} style={{ 
                    background: '#1d1f27', 
                    borderRadius: '12px', 
                    overflow: 'hidden',
                    border: '1px solid rgba(255,255,255,0.05)',
                    boxShadow: '0 10px 30px rgba(0,0,0,0.3)'
                  }}>
                    <div style={{ 
                      background: '#15171e', 
                      padding: '0.75rem 1.25rem', 
                      fontSize: '0.85rem', 
                      color: '#a0a0b0', 
                      display: 'flex', 
                      justifyContent: 'space-between',
                      borderBottom: '1px solid rgba(255,255,255,0.05)'
                    }}>
                      <span style={{ fontFamily: 'Fira Code, monospace', color: '#fff' }}>{artifact.filename}</span>
                      <span style={{ textTransform: 'uppercase', fontSize: '0.75rem', letterSpacing: '0.05em' }}>{artifact.language}</span>
                    </div>
                    <div style={{ maxHeight: '500px', overflow: 'auto', background: '#1d1f27' }}>
                      <pre className={`language-${artifact.language || 'python'}`} style={{ margin: 0, padding: '1.25rem', background: 'transparent' }}>
                        <code className={`language-${artifact.language || 'python'}`}>{artifact.content}</code>
                      </pre>
                    </div>
                  </motion.div>
                ))}
                {(!state.generated_artifacts || state.generated_artifacts.length === 0) && (
                  <div className="empty-state" style={{ height: '100%' }}>No artifacts engineered.</div>
                )}
              </div>
            )}

            {activeTab === 'logs' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', background: '#0d0d12', padding: '1rem', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)', minHeight: '100%' }}>
                {state.execution_logs?.map((log, idx) => (
                  <div key={idx} className="code-font" style={{ 
                    fontSize: '0.85rem',
                    color: log.level === 'ERROR' ? '#f87171' : log.level === 'WARNING' ? '#fbbf24' : '#a0a0b0',
                    padding: '0.25rem 0',
                    lineHeight: 1.4
                  }}>
                    <span style={{ opacity: 0.4, marginRight: '0.75rem' }}>{log.timestamp.split('T')[1]?.split('.')[0] || log.timestamp}</span>
                    <span>{log.message}</span>
                  </div>
                ))}
                <div ref={logsEndRef} />
                {(!state.execution_logs || state.execution_logs.length === 0) && (
                  <div className="code-font" style={{ opacity: 0.5 }}>$ awaiting output...</div>
                )}
              </div>
            )}

            {activeTab === 'evaluation' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', height: '100%', justifyContent: 'center', alignItems: 'center' }}>
                {state.evaluation ? (
                  <motion.div variants={itemVariants} style={{
                    background: state.evaluation.passed ? 'rgba(16, 185, 129, 0.05)' : 'rgba(239, 68, 68, 0.05)',
                    border: `1px solid ${state.evaluation.passed ? 'rgba(16, 185, 129, 0.2)' : 'rgba(239, 68, 68, 0.2)'}`,
                    padding: '3rem',
                    borderRadius: '24px',
                    textAlign: 'center',
                    maxWidth: '500px',
                    boxShadow: state.evaluation.passed ? '0 0 40px rgba(16, 185, 129, 0.1)' : '0 0 40px rgba(239, 68, 68, 0.1)'
                  }}>
                    
                    <div style={{ position: 'relative', width: '120px', height: '120px', margin: '0 auto 2rem auto' }}>
                      <svg width="120" height="120" viewBox="0 0 120 120">
                        <circle cx="60" cy="60" r="54" fill="none" stroke="rgba(255,255,255,0.1)" strokeWidth="8" />
                        <motion.circle 
                          cx="60" cy="60" r="54" fill="none" 
                          stroke={state.evaluation.passed ? 'var(--accent-green)' : 'var(--accent-red)'} 
                          strokeWidth="8"
                          strokeLinecap="round"
                          initial={{ strokeDasharray: "0 339" }}
                          animate={{ strokeDasharray: `${(state.evaluation.score / 100) * 339} 339` }}
                          transition={{ duration: 1.5, ease: "easeOut" }}
                          transform="rotate(-90 60 60)"
                        />
                      </svg>
                      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '2rem', fontWeight: 700, color: '#fff' }}>
                        {state.evaluation.score}
                      </div>
                    </div>

                    <h3 style={{ color: state.evaluation.passed ? '#34d399' : '#f87171', marginBottom: '1rem', fontSize: '1.5rem', letterSpacing: '0.05em' }}>
                      {state.evaluation.passed ? 'VERIFIED' : 'FAILED'}
                    </h3>
                    <p style={{ color: '#a0a0b0', fontSize: '1rem', lineHeight: 1.6 }}>{state.evaluation.summary}</p>
                  </motion.div>
                ) : (
                  <div className="empty-state">System evaluation pending...</div>
                )}
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
};
