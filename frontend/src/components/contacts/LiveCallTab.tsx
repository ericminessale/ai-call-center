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
  const chipClass = score > 0.3
    ? 'chip-live'
    : score < -0.3
    ? 'chip-urgent'
    : 'chip-muted';

  return (
    <span className={`chip ${chipClass}`} title={sentiment.reason || label}>
      <Icon className="w-2.5 h-2.5" />
      <span>{label}</span>
      <span className="opacity-70">({score > 0 ? '+' : ''}{score.toFixed(1)})</span>
    </span>
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
      if (callState === 'ringing') return { text: 'Calling', dotClass: 'dot dot-wait', textClass: 'text-wait-soft' };
      if (callState === 'ending') return { text: 'Ending', dotClass: 'dot dot-offline', textClass: 'text-ink-dim' };
    }
    if (callState === 'active') return { text: 'Connected', dotClass: 'dot dot-live', textClass: 'text-live-soft' };
    return { text: 'Recording', dotClass: 'dot dot-live', textClass: 'text-live-soft' };
  };

  const status = getStatusDisplay();

  return (
    <div className="h-full flex flex-col">
      {/* AI Context Panel */}
      {hasContext(aiContext) && (
        <div className="px-5 pt-4">
          <AgentContextCard context={aiContext!} variant="full" collapsible />
        </div>
      )}

      {/* Call Event Stream */}
      {callSid && (
        <div className="px-5 pt-3">
          <CallEventStream callId={callSid} callSid={callSid} />
        </div>
      )}

      {/* Live Transcription */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-5 h-11 border-b border-rule bg-canvas-sunken/60 sticky top-0 z-10">
          <div>
            <div className="kicker mb-0.5">
              {isOutboundCallInProgress && callState === 'ringing' ? 'Outbound call' : 'Live feed'}
            </div>
            <h3 className="font-display text-[18px] text-ink leading-none">
              {isAICall ? 'Agent is speaking with the caller' : 'Transcription'}
            </h3>
          </div>
          <div className="flex items-center gap-2">
            {sentiment && <SentimentIndicator sentiment={sentiment} />}
            <div className={`flex items-center gap-1.5 text-[11.5px] font-medium mono uppercase tracking-wider ${status.textClass}`}>
              <span className={status.dotClass} />
              {status.text}
            </div>
          </div>
        </div>
        <div className="flex-1 px-5 pt-4 pb-5 overflow-y-auto">
          <div
            ref={transcriptionContainerRef}
            className="relative bg-canvas-sunken border border-rule rounded-md min-h-[320px] max-h-[440px] overflow-y-auto p-5"
          >
            {isOutboundCallInProgress && callState === 'ringing' && transcription.length === 0 ? (
              <div className="flex items-center justify-center h-64">
                <div className="text-center">
                  <PhoneOutgoing className="w-6 h-6 mx-auto mb-3 text-wait-soft animate-pulse" />
                  <p className="font-display text-[22px] text-wait-soft leading-none">Calling…</p>
                  <p className="text-[12px] text-ink-dim mt-2 mono uppercase tracking-wider">Waiting for answer</p>
                </div>
              </div>
            ) : transcription.length > 0 ? (
              <div className="space-y-4">
                {transcription.map((entry, idx) => {
                  const isAgent = entry.speaker === 'agent' || entry.speaker === 'ai';
                  return (
                    <div key={entry.id || idx} className="flex gap-3 animate-fade-up">
                      <div className="shrink-0 pt-0.5 w-16">
                        <span className={`kicker ${
                          entry.speaker === 'agent' ? 'text-live-soft' :
                          entry.speaker === 'ai' ? 'text-ai-soft' :
                          'text-info-soft'
                        }`} style={{ color: 'inherit' }}>
                          {entry.speaker === 'agent' ? 'Agent' : entry.speaker === 'ai' ? 'AI' : 'Caller'}
                        </span>
                        <div className="mono text-[9.5px] text-ink-faint mt-0.5">
                          {new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </div>
                      </div>
                      <div className={`flex-1 text-[13px] leading-relaxed pl-3 border-l ${
                        isAgent ? 'border-ai/30' : 'border-info/30'
                      } text-ink`}>
                        {entry.text}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="flex items-center justify-center h-64">
                <div className="text-center">
                  <Mic className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
                  <p className="font-display text-[20px] text-ink-muted leading-none">Listening…</p>
                  <p className="text-[12px] text-ink-dim mt-2 mono uppercase tracking-wider">
                    Transcript will stream here
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* AI Message Controls */}
      {isAICall && (
        <div className="border-t border-rule px-5 py-4 bg-canvas-sunken">
          <div className="flex items-center gap-2 mb-3">
            <Bot className="w-3.5 h-3.5 text-ai-soft" />
            <span className="kicker" style={{ color: '#B0A4FF' }}>Whisper to AI</span>
            <span className="text-[11.5px] text-ink-dim ml-1">
              Inject a system instruction mid-call.
            </span>
          </div>

          <div className="flex flex-wrap gap-1.5 mb-3">
            {quickTemplates.map((template, idx) => (
              <button
                key={idx}
                onClick={() => setSystemMessage(template.message)}
                className="text-[11.5px] px-2.5 py-1 rounded bg-ai/10 text-ai-soft border border-ai/25 hover:bg-ai/15 transition-colors"
                disabled={isSending}
              >
                {template.label}
              </button>
            ))}
          </div>

          <div className="flex gap-2">
            <input
              type="text"
              value={systemMessage}
              onChange={(e) => setSystemMessage(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') sendSystemMessage(); }}
              placeholder="Tell the AI what to do next…"
              className="input flex-1"
              disabled={isSending}
            />
            <button
              onClick={sendSystemMessage}
              disabled={!systemMessage.trim() || isSending}
              className="btn-secondary !border-ai/30 !text-ai-soft hover:!bg-ai/10 disabled:opacity-50"
            >
              {isSending ? (
                <div className="animate-spin rounded-full h-3.5 w-3.5 border-2 border-ai-soft border-t-transparent" />
              ) : (
                <Send className="w-3.5 h-3.5" />
              )}
              Send
            </button>
          </div>

          {error && (
            <div className="mt-2 p-2 bg-urgent/10 border border-urgent/30 rounded text-[11.5px] text-urgent-soft flex items-center gap-2">
              <AlertCircle className="w-3 h-3 flex-shrink-0" />
              {error}
            </div>
          )}
          {success && (
            <div className="mt-2 p-2 bg-live/10 border border-live/30 rounded text-[11.5px] text-live-soft flex items-center gap-2">
              <Bot className="w-3 h-3 flex-shrink-0" />
              Whisper delivered to the AI.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
