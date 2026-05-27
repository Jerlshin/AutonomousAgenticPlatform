import { useState } from 'react';
import { Send, Loader2 } from 'lucide-react';

interface WorkflowInputProps {
  onSubmit: (request: string) => void;
  isLoading: boolean;
}

export const WorkflowInput = ({ onSubmit, isLoading }: WorkflowInputProps) => {
  const [request, setRequest] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (request.trim() && !isLoading) {
      onSubmit(request);
      setRequest('');
    }
  };

  return (
    <form onSubmit={handleSubmit} className="input-form">
      <textarea
        className="task-input"
        placeholder="Describe your AI R&D task here... e.g. 'Write a python script that fetches the latest news and performs sentiment analysis using an LLM.'"
        value={request}
        onChange={(e) => setRequest(e.target.value)}
        disabled={isLoading}
      />
      <button 
        type="submit" 
        className="submit-btn" 
        disabled={isLoading || !request.trim()}
      >
        {isLoading ? (
          <>
            <Loader2 className="animate-pulse-glow" size={18} />
            Initializing Workflow...
          </>
        ) : (
          <>
            <Send size={18} />
            Start Workflow
          </>
        )}
      </button>
    </form>
  );
};
