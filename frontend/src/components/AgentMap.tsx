import { Activity, BrainCircuit, Code, Play, Bug, BarChart, CheckCircle2 } from 'lucide-react';

interface AgentMapProps {
  currentNode: string | null;
}

const AGENTS = [
  { id: 'planner', label: 'Planner', icon: BrainCircuit },
  { id: 'research_agent', label: 'Researcher', icon: Activity },
  { id: 'coder', label: 'Coder', icon: Code },
  { id: 'executor', label: 'Executor', icon: Play },
  { id: 'debugger', label: 'Debugger', icon: Bug },
  { id: 'mlops', label: 'MLOps', icon: BarChart },
  { id: 'evaluator', label: 'Evaluator', icon: CheckCircle2 },
];

export const AgentMap = ({ currentNode }: AgentMapProps) => {
  // Determine if a node is past, present, or future
  const currentIndex = AGENTS.findIndex(a => a.id === currentNode);

  return (
    <div className="glass-panel" style={{ padding: '1.5rem', flex: 1, display: 'flex', flexDirection: 'column' }}>
      <h3 style={{ fontSize: '1.1rem', fontWeight: 600, marginBottom: '1.5rem' }}>Agent Workforce Map</h3>
      
      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', flex: 1 }}>
        {AGENTS.map((agent, index) => {
          const isActive = agent.id === currentNode;
          const isPast = currentIndex !== -1 && index < currentIndex;
          const isError = currentNode === 'Error';
          
          let statusColor = 'var(--text-secondary)';
          let bgColor = 'rgba(255,255,255,0.05)';
          let glow = 'none';

          if (isActive) {
            statusColor = 'var(--accent-blue)';
            bgColor = 'var(--accent-blue-glow)';
            glow = '0 0 15px var(--accent-blue-glow)';
          } else if (isPast || currentNode === 'Finished') {
            statusColor = 'var(--accent-green)';
            bgColor = 'var(--accent-green-glow)';
          } else if (isError && isActive) {
            statusColor = 'var(--accent-red)';
            bgColor = 'var(--accent-red-glow)';
          }

          const Icon = agent.icon;

          return (
            <div key={agent.id} style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
              <div style={{
                width: '40px',
                height: '40px',
                borderRadius: '50%',
                background: bgColor,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: statusColor,
                boxShadow: glow,
                transition: 'all 0.3s ease'
              }} className={isActive ? 'animate-pulse-glow' : ''}>
                <Icon size={20} />
              </div>
              
              <div style={{ flex: 1 }}>
                <div style={{ 
                  fontWeight: isActive ? 600 : 500, 
                  color: isActive ? '#fff' : (isPast ? '#ccc' : 'var(--text-secondary)'),
                  fontSize: '0.95rem'
                }}>
                  {agent.label}
                </div>
              </div>

              {/* Connecting line to next node except for last item */}
              {index < AGENTS.length - 1 && (
                <div style={{
                  position: 'absolute',
                  width: '2px',
                  height: '1.5rem',
                  background: isPast ? 'var(--accent-green)' : 'rgba(255,255,255,0.1)',
                  marginLeft: '19px',
                  marginTop: '45px',
                  zIndex: 0
                }} />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
