import { Activity, BrainCircuit, Code, Play, Bug, BarChart, CheckCircle2 } from 'lucide-react';
import { motion } from 'framer-motion';

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
  const currentIndex = AGENTS.findIndex(a => a.id === currentNode);

  return (
    <div className="glass-panel" style={{ padding: '1.5rem', flex: 1, display: 'flex', flexDirection: 'column', position: 'relative' }}>
      <h3 style={{ fontSize: '1.1rem', fontWeight: 600, marginBottom: '2rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--accent-blue)', boxShadow: '0 0 10px var(--accent-blue)' }} />
        Agent Map
      </h3>
      
      <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem', flex: 1, position: 'relative', zIndex: 1 }}>
        {AGENTS.map((agent, index) => {
          const isActive = agent.id === currentNode;
          const isPast = currentIndex !== -1 && index < currentIndex;
          const isError = currentNode === 'Error' && isActive;
          
          let statusColor = 'var(--text-secondary)';
          let bgColor = 'rgba(255,255,255,0.03)';
          let borderColor = 'rgba(255,255,255,0.05)';

          if (isActive) {
            statusColor = 'var(--accent-blue)';
            bgColor = 'var(--accent-blue-glow)';
            borderColor = 'var(--accent-blue)';
          } else if (isPast || currentNode === 'Finished') {
            statusColor = 'var(--accent-green)';
            bgColor = 'var(--accent-green-glow)';
            borderColor = 'var(--accent-green)';
          } else if (isError) {
            statusColor = 'var(--accent-red)';
            bgColor = 'var(--accent-red-glow)';
            borderColor = 'var(--accent-red)';
          }

          const Icon = agent.icon;

          return (
            <div key={agent.id} style={{ display: 'flex', alignItems: 'center', gap: '1.25rem', position: 'relative' }}>
              {/* Connecting Line to next node */}
              {index < AGENTS.length - 1 && (
                <div style={{
                  position: 'absolute',
                  left: '21px',
                  top: '42px',
                  width: '2px',
                  height: '1.5rem',
                  background: isPast ? 'var(--accent-green)' : 'rgba(255,255,255,0.1)',
                  zIndex: -1,
                  overflow: 'hidden'
                }}>
                  {isActive && (
                    <motion.div
                      initial={{ y: '-100%' }}
                      animate={{ y: '100%' }}
                      transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                      style={{ width: '100%', height: '50%', background: 'var(--accent-blue)', boxShadow: '0 0 8px var(--accent-blue)' }}
                    />
                  )}
                </div>
              )}

              <motion.div 
                animate={isActive ? { scale: [1, 1.1, 1], boxShadow: ['0 0 0px transparent', '0 0 20px var(--accent-blue-glow)', '0 0 0px transparent'] } : {}}
                transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
                style={{
                  width: '44px',
                  height: '44px',
                  borderRadius: '12px',
                  background: bgColor,
                  border: `1px solid ${borderColor}`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: statusColor,
                  zIndex: 2,
                  boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.1)'
                }}
              >
                <Icon size={20} />
              </motion.div>
              
              <div style={{ flex: 1 }}>
                <div style={{ 
                  fontWeight: isActive ? 600 : 500, 
                  color: isActive ? '#fff' : (isPast ? '#e2e8f0' : 'var(--text-secondary)'),
                  fontSize: '0.95rem',
                  letterSpacing: '0.01em'
                }}>
                  {agent.label}
                </div>
                {(isActive || isPast) && (
                  <motion.div 
                    initial={{ opacity: 0, height: 0 }} 
                    animate={{ opacity: 1, height: 'auto' }}
                    style={{ fontSize: '0.75rem', color: statusColor, marginTop: '2px' }}
                  >
                    {isActive ? 'Processing...' : 'Completed'}
                  </motion.div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
