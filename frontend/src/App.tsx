import { useState, useRef, useEffect } from 'react';
import { Settings, Activity, ShieldAlert } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import './App.css';
import type { AgentState } from './types';
import { WorkflowInput } from './components/WorkflowInput';
import { AgentProgress } from './components/AgentProgress';
import { StateVisualizer } from './components/StateVisualizer';
import { AgentMap } from './components/AgentMap';

export default function App() {
  const [state, setState] = useState<AgentState | null>(null);
  const [currentNode, setCurrentNode] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  const startWorkflow = (request: string) => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    
    setIsLoading(true);
    setState(null);
    setCurrentNode('Initializing...');

    const ws = new WebSocket('ws://localhost:8000/workflows/ws');
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ user_request: request, max_retries: 2 }));
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.node) setCurrentNode(data.node);
        if (data.state) setState(data.state);
        setIsLoading(false);
      } catch (err) {
        console.error("Failed to parse WS message", err);
      }
    };

    ws.onclose = () => {
      if (currentNode !== 'Terminated' && currentNode !== 'Finished') {
        setCurrentNode('Finished');
      }
      setIsLoading(false);
    };

    ws.onerror = (error) => {
      console.error("WebSocket Error:", error);
      setCurrentNode('Error');
      setIsLoading(false);
    };
  };

  const terminateWorkflow = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setCurrentNode('Terminated');
    setIsLoading(false);
  };

  const submitHumanInput = (inputValue: string) => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({ human_input: inputValue || "NO" }));
    }
  };

  return (
    <div className="app-layout">
      {/* Top Navigation Bar */}
      <header className="top-bar">
        <div className="logo-area">
          <Activity className="logo-icon" size={24} />
          <span>AutoAI Platform</span>
        </div>
        
        {isLoading && (
          <button 
            className="submit-btn terminate" 
            onClick={terminateWorkflow}
            style={{ padding: '0.4rem 1rem', fontSize: '0.85rem' }}
          >
            <ShieldAlert size={16} />
            Terminate Workflow
          </button>
        )}
      </header>

      {/* Human-in-the-loop Overlay */}
      <AnimatePresence>
        {state?.status === 'waiting_for_input' && (
          <motion.div 
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="hitl-overlay"
          >
            <div className="hitl-modal glass-panel">
              <h3 style={{ marginTop: 0, color: 'var(--accent-blue)', display: 'flex', alignItems: 'center', gap: '0.75rem', fontSize: '1.25rem' }}>
                <ShieldAlert size={24} />
                Agent Intervention Required
              </h3>
              <p style={{ color: '#e2e8f0', marginBottom: '1.5rem', lineHeight: 1.6, fontSize: '1.05rem' }}>
                {state.human_query}
              </p>
              <form onSubmit={(e) => {
                e.preventDefault();
                const val = (e.currentTarget.elements.namedItem('human_input') as HTMLInputElement).value;
                submitHumanInput(val);
              }}>
                <input 
                  name="human_input"
                  type="text" 
                  placeholder="Enter value or type NO..." 
                  style={{ 
                    width: '100%', padding: '1rem', borderRadius: '8px', 
                    border: '1px solid rgba(59, 130, 246, 0.3)', 
                    background: 'rgba(0,0,0,0.5)', color: '#fff', 
                    marginBottom: '1.5rem', fontSize: '1rem',
                    boxShadow: 'inset 0 2px 4px rgba(0,0,0,0.5)'
                  }}
                  autoFocus
                />
                <div style={{ display: 'flex', gap: '1rem', justifyContent: 'flex-end' }}>
                  <button type="button" onClick={() => submitHumanInput("NO")} className="tab-btn" style={{ background: 'rgba(255,255,255,0.05)', color: '#ccc' }}>
                    Decline / Skip
                  </button>
                  <button type="submit" className="submit-btn" style={{ padding: '0.75rem 2rem' }}>
                    Confirm Input
                  </button>
                </div>
              </form>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 3-Panel Main Layout */}
      <div className="app-container">
        {/* Left Panel: Agent Map */}
        <aside className="sidebar-left">
          <AgentMap currentNode={currentNode} />
        </aside>

        {/* Center Panel: Visualization Feed */}
        <main className="main-content">
          <StateVisualizer state={state} />
        </main>

        {/* Right Panel: Status & Input */}
        <aside className="sidebar-right">
          <AgentProgress state={state} currentNode={currentNode} />
          
          <div style={{ flex: 1 }} />
          
          <WorkflowInput onSubmit={startWorkflow} isLoading={isLoading} />
          
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--text-secondary)', fontSize: '0.85rem', marginTop: '1rem', justifyContent: 'center' }}>
            <Settings size={14} />
            <span>Local Engine Active</span>
          </div>
        </aside>
      </div>
    </div>
  );
}
