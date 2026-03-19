import { useState, useEffect, useRef } from 'react';
import { PhoneOutgoing, Bot, Mic, Send, AlertCircle, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { TranscriptionMessage } from '../../types/callcenter';
import api from '../../services/api';
import { logger } from '../../lib/logger';
import { AgentContextCard, hasContext } from '../shared/AgentContextCard';
import CallEventStream from './CallEventStream';

export interface AIContext {
  customer_name?: string;
  account_number?: string;
  reason?: string;
  issue?: string;
  issue_description?: string;
  urgency?: string;
  priority?: number;
  department?: string;
  interest?: string;
  company?: string;
  budget?: string;
  error_message?: string;
  additional_info?: string;
  ai_summary?: string;
  source_agent?: string;
  preferred_handling?: 'ai' | 'human';
  queue?: string;
  global_data?: Record<string, any>;
}

export interface SentimentData {
  score: number;       // -1.0 to 1.0
  reason?: string;     // What triggered the change
  timestamp?: string;  // ISO timestamp
}

interface LiveCallTabProps {
  transcription: TranscriptionMessage[];
  isAICall: boolean;
  callSid?: string;
  callDuration?: number;
  callState?: 'idle' | 'ringing' | 'active' | 'ending';
  isOutboundCallInProgress?: boolean;
  aiContext?: AIContext;
  sentiment?: SentimentData | null;
}

function SentimentIndicator({ sentiment }: { sentiment: SentimentData }) {
  const score = sentiment.score;
  const label = score > 0.3 ? 'Positive' : score < -0.3 ? 'Negative' : 'Neutral';
  const Icon = score > 0.3 ? TrendingUp : score < -0.3 ? TrendingDown : Minus;
  const colorClass = score > 0.3
    ? 'text-green-400 bg-green-500/15 border-green-500/30'
    : score < -0.3
    ? 'text-red-400 bg-red-500/15 border-red-500/30'
    : 'text-gray-400 bg-gray-500/15 border-gray-500/30';

  return (
    <div className={`flex items-center gap-1.5 px-2 py-1 rounded-md border text-xs font-medium ${colorClass}`} title={sentiment.reason || label}>
      <Icon className="w-3 h-3" />
      <span>{label}</span>
      <span className="opacity-70">({score > 0 ? '+' : ''}{score.toFixed(1)})</span>
    </div>
  );
}

export function LiveCallTab({
  transcription,
  isAICall,
  callSid,
  callDuration,
  callState,
  isOutboundCallInProgress,
  aiContext,
  sentiment,
}: LiveCallTabProps) {
  const [systemMessage, setSystemMessage] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const transcriptionContainerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll transcription container to bottom when new lines arrive
  useEffect(() => {
    const container = transcriptionContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [transcription]);

  const quickTemplates = [
    { label: 'Offer Discount', message: 'The customer qualifies for a 20% discount. Offer this to help close the sale.' },
    { label: 'Transfer to Human', message: 'This customer needs specialized help. Transfer them to a human agent now.' },
    { label: 'Apologize', message: 'Acknowledge the customer\'s frustration with empathy and apologize for any inconvenience.' },
    { label: 'Gather Details', message: 'Ask more specific questions to better understand the customer\'s needs.' },
  ];

  const sendSystemMessage = async () => {
    if (!systemMessage.trim()) return;
    if (!callSid) {
      setError('No active call SID available');
      return;
    }

    setIsSending(true);
    setError(null);
    setSuccess(false);

    try {
      await api.post('/api/ai/inject-message', {
        call_id: callSid,
        message: systemMessage,
        role: 'system'
      });

      setSuccess(true);
      setSystemMessage('');
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: any) {
      logger.error('[AI Message] Failed:', err.response?.data || err);
      setError(err.response?.data?.error || 'Failed to send message to AI agent');
    } finally {
      setIsSending(false);
    }
  };

  const getStatusDisplay = () => {
    if (isOutboundCallInProgress) {
      if (callState === 'ringing') return { text: 'Calling...', color: 'text-yellow-400', bgColor: 'bg-yellow-500' };
      if (callState === 'ending') return { text: 'Ending...', color: 'text-gray-400', bgColor: 'bg-gray-500' };
    }
    if (callState === 'active') return { text: 'Connected', color: 'text-green-400', bgColor: 'bg-green-500' };
    return { text: 'Recording', color: 'text-green-400', bgColor: 'bg-green-500' };
  };

  const status = getStatusDisplay();

  return (
    <div className="h-full flex flex-col">
      {/* AI Context Panel - Shows data collected by AI agent (ABOVE transcription) */}
      {hasContext(aiContext) && (
        <div className="p-4 pb-0">
          <AgentContextCard context={aiContext!} variant="full" collapsible />
        </div>
      )}

      {/* Call Event Stream */}
      {callSid && (
        <div className="px-4 pt-2">
          <CallEventStream callId={callSid} callSid={callSid} />
        </div>
      )}

      {/* Live Transcription */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700 bg-gray-800/80 sticky top-0 z-10">
          <h3 className="text-lg font-semibold text-white">
            {isOutboundCallInProgress && callState === 'ringing' ? 'Outbound Call' : 'Live Transcription'}
          </h3>
          <div className="flex items-center gap-3">
            {sentiment && <SentimentIndicator sentiment={sentiment} />}
            <div className={`flex items-center gap-2 ${status.color} text-sm`}>
              <div className={`w-2 h-2 ${status.bgColor} rounded-full animate-pulse`} />
              {status.text}
            </div>
          </div>
        </div>
        <div className="flex-1 p-4 overflow-y-auto">

        <div ref={transcriptionContainerRef} className="bg-gray-900 rounded-lg p-4 min-h-[300px] max-h-[400px] overflow-y-auto font-mono text-sm">
          {isOutboundCallInProgress && callState === 'ringing' && transcription.length === 0 ? (
            <div className="flex items-center justify-center h-64 text-gray-500">
              <div className="text-center">
                <PhoneOutgoing className="w-12 h-12 mx-auto mb-2 text-yellow-400 animate-pulse" />
                <p className="text-yellow-400 font-medium">Calling...</p>
                <p className="text-gray-500 text-sm mt-1">Waiting for answer</p>
              </div>
            </div>
          ) : transcription.length > 0 ? (
            <div className="space-y-3">
              {transcription.map((entry, idx) => (
                <div key={entry.id || idx} className="flex flex-col space-y-1">
                  <div className="flex items-center space-x-2">
                    <span className={`font-semibold ${
                      entry.speaker === 'agent' || entry.speaker === 'ai' ? 'text-purple-400' : 'text-blue-400'
                    }`}>
                      {entry.speaker === 'agent' ? 'Agent:' : entry.speaker === 'ai' ? 'AI:' : 'Caller:'}
                    </span>
                    <span className="text-xs text-gray-500">
                      {new Date(entry.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <p className="text-gray-300 pl-4">{entry.text}</p>
                </div>
              ))}
              {/* scroll anchor removed - container scrolls via ref */}
            </div>
          ) : (
            <div className="flex items-center justify-center h-64 text-gray-500">
              <div className="text-center">
                <Mic className="w-12 h-12 mx-auto mb-2 opacity-50" />
                <p>Waiting for conversation...</p>
              </div>
            </div>
          )}
        </div>
        </div>
      </div>

      {/* AI Message Controls - Only for AI calls */}
      {isAICall && (
        <div className="border-t border-gray-700 p-4 bg-gray-800">
          <div className="bg-purple-900/30 border border-purple-500/30 rounded-lg p-3 mb-3">
            <div className="flex items-start gap-2">
              <Bot className="w-4 h-4 text-purple-400 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-purple-300">
                Send instructions to guide the AI agent's behavior during this call
              </p>
            </div>
          </div>

          {/* Quick Templates */}
          <div className="flex flex-wrap gap-2 mb-3">
            {quickTemplates.map((template, idx) => (
              <button
                key={idx}
                onClick={() => setSystemMessage(template.message)}
                className="text-xs px-3 py-1.5 bg-purple-500/20 text-purple-300 rounded-md hover:bg-purple-500/30 transition-colors"
                disabled={isSending}
              >
                {template.label}
              </button>
            ))}
          </div>

          {/* Message Input */}
          <div className="flex gap-2">
            <input
              type="text"
              value={systemMessage}
              onChange={(e) => setSystemMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') sendSystemMessage();
              }}
              placeholder="Type message to AI agent..."
              className="flex-1 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-400 focus:outline-none focus:border-purple-500"
              disabled={isSending}
            />
            <button
              onClick={sendSystemMessage}
              disabled={!systemMessage.trim() || isSending}
              className={`px-4 py-2 rounded-lg font-medium transition-colors flex items-center gap-2 ${
                systemMessage.trim() && !isSending
                  ? 'bg-purple-600 text-white hover:bg-purple-700'
                  : 'bg-gray-600 text-gray-400 cursor-not-allowed'
              }`}
            >
              {isSending ? (
                <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              Send
            </button>
          </div>

          {/* Status Messages */}
          {error && (
            <div className="mt-2 p-2 bg-red-500/20 border border-red-500/50 rounded-lg text-xs text-red-400 flex items-center gap-2">
              <AlertCircle className="w-3 h-3 flex-shrink-0" />
              {error}
            </div>
          )}
          {success && (
            <div className="mt-2 p-2 bg-green-500/20 border border-green-500/50 rounded-lg text-xs text-green-400 flex items-center gap-2">
              <Bot className="w-3 h-3 flex-shrink-0" />
              Message sent to AI agent successfully!
            </div>
          )}
        </div>
      )}
    </div>
  );
}
