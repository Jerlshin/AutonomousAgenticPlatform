import { useState, useRef, useEffect } from 'react';
import { Settings, Activity, ShieldAlert } from 'lucide-react';
import './App.css';
import type { AgentState } from './types';
import { WorkflowInput } from './components/WorkflowInput';
import { AgentProgress } from './components/AgentProgress';
import { StateVisualizer } from './components/StateVisualizer';
import { AgentMap } from './components/AgentMap';

function App() {
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

export default App;
