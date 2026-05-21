import { useState, useEffect, useRef } from 'react';
import { PhoneOutgoing, Bot, Mic, Send, AlertCircle, TrendingUp, TrendingDown, Minus, BookOpen, ChevronDown, Search, MessageSquare, Sparkles, HelpCircle } from 'lucide-react';
import { TranscriptionMessage } from '../../types/callcenter';
import api from '../../services/api';
import websocket from '../../services/websocket';
import { logger } from '../../lib/logger';
import { useAuthStore } from '../../stores/authStore';
import { usePermissions } from '../../hooks/usePermissions';
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

interface FactbookResult {
  content: string;
  filename: string;
  section: string;
  metadata: Record<string, any>;
  score: number;
}

// One entry in the Coach suggestion feed. The backend webhook
// (/api/webhooks/sidecar/events) emits `coaching_suggestion` events
// with this shape — see backend/app/api/webhooks.py:sidecar_events for
// the canonical schema. We keep the raw payload around for debug but
// only the trimmed fields here are rendered.
interface CoachSuggestion {
  id: string;             // local UUID-ish — kind+timestamp+text hash
  kind: string;           // 'suggestion' | 'ask_answer' | 'tool_call' | ...
  text: string;           // human-readable suggestion / answer
  timestamp: number;      // ms epoch when received
  ask_id?: string;        // correlation id for explicit asks (M10)
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

  const factbookMode = useAuthStore((s) => s.user?.kb_factbook_mode) || 'manual';
  const factbookEnabled = factbookMode !== 'off';

  const [factbookOpen, setFactbookOpen] = useState(false);
  const [factbookQuery, setFactbookQuery] = useState('');
  const [factbookResults, setFactbookResults] = useState<FactbookResult[]>([]);
  const [factbookLoading, setFactbookLoading] = useState(false);
  const [factbookError, setFactbookError] = useState<string | null>(null);
  const [factbookNote, setFactbookNote] = useState<string | null>(null);
  const [factbookSearched, setFactbookSearched] = useState(false);

  // AI Coach (sidecar) — sibling surface to the Factbook. Whether the panel
  // is even visible is the `can_use_coach` permission (admin ceiling). Mode
  // is a per-call agent decision, not a stored preference, so it lives in
  // local state and resets between calls. See AGENT_ASSIST.md.
  type CoachMode = 'off' | 'on_request' | 'auto';
  const { can } = usePermissions();
  const coachAllowed = can('can_use_coach');

  const [coachMode, setCoachMode] = useState<CoachMode>('off');
  // Tracks whether a sidecar is actually attached on the backend. Distinct
  // from `coachMode` because attach is async — between click and 2xx the
  // mode is "pending"; we don't want to immediately render the new mode's
  // UI until the backend confirms.
  const [coachAttaching, setCoachAttaching] = useState(false);
  const [coachAttachError, setCoachAttachError] = useState<string | null>(null);
  const [coachOpen, setCoachOpen] = useState(false);
  const [coachSuggestions, setCoachSuggestions] = useState<CoachSuggestion[]>([]);
  const [coachAskInput, setCoachAskInput] = useState('');
  const [coachAsking, setCoachAsking] = useState(false);
  const [coachAskError, setCoachAskError] = useState<string | null>(null);
  const coachFeedRef = useRef<HTMLDivElement>(null);

  const coachAttached = coachMode !== 'off';

  // Auto-scroll transcription container to bottom when new lines arrive
  useEffect(() => {
    const container = transcriptionContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [transcription]);

  // Factbook auto-mode: subscribe to backend-pushed kb_fact events that fire
  // on each caller turn-end. The panel auto-expands so the agent sees facts
  // appear without having to open it first.
  useEffect(() => {
    if (factbookMode !== 'auto' || !callSid) return;
    const handler = (data: { query?: string; results?: FactbookResult[] }) => {
      if (Array.isArray(data?.results)) {
        setFactbookResults(data.results);
        setFactbookQuery(data.query || '');
        setFactbookSearched(true);
        setFactbookOpen(true);
        setFactbookError(null);
        setFactbookNote(null);
      }
    };
    websocket.on('kb_fact', handler);
    return () => websocket.off('kb_fact', handler);
  }, [factbookMode, callSid]);

  // AI Coach — subscribe to `coaching_suggestion` events. Both `auto` and
  // `on_request` modes receive the same event channel; the difference is
  // who triggers the suggestion (sidecar vs. agent's ask). We auto-open
  // the panel on the first real suggestion so the agent doesn't miss it.
  useEffect(() => {
    if (!coachAllowed || !callSid) return;
    const handler = (data: CoachSuggestion & { call_sid?: string }) => {
      // Ignore stray events from other calls (event is also room-scoped on
      // the backend, but defense-in-depth costs nothing).
      if (data.call_sid && data.call_sid !== callSid) return;
      // Skip `skip` events — those are the sidecar's "no suggestion this
      // turn" signal. Useful in logs, useless in the UI.
      if (data.kind === 'skip') return;

      const entry: CoachSuggestion = {
        // Backend doesn't always provide a stable id; synth a local one.
        id: `${data.kind}-${data.timestamp || Date.now()}-${
          (data.text || '').slice(0, 16)
        }`,
        kind: data.kind || 'suggestion',
        text: data.text || '',
        timestamp: typeof data.timestamp === 'number' ? data.timestamp : Date.now(),
        ask_id: data.ask_id,
      };
      // Drop empty-text suggestions silently — those are usually tool_call
      // pre-events (we'll surface those when M11 lands the lookup_kb tool).
      if (!entry.text) return;
      setCoachSuggestions((prev) => [...prev, entry]);
      setCoachOpen(true);
      setCoachAskError(null);
    };
    websocket.on('coaching_suggestion', handler);
    return () => websocket.off('coaching_suggestion', handler);
  }, [coachAllowed, callSid]);

  // Auto-scroll the coach feed to the latest suggestion as they stream in.
  useEffect(() => {
    if (coachFeedRef.current) {
      coachFeedRef.current.scrollTop = coachFeedRef.current.scrollHeight;
    }
  }, [coachSuggestions]);

  // Reset coach state when the call changes — old suggestions from a
  // previous call would be confusing if they lingered, and the new call
  // has no sidecar attached yet (mode resets to 'off').
  useEffect(() => {
    setCoachSuggestions([]);
    setCoachAskInput('');
    setCoachAskError(null);
    setCoachOpen(false);
    setCoachMode('off');
    setCoachAttachError(null);
  }, [callSid]);

  const changeCoachMode = async (nextMode: CoachMode) => {
    if (!callSid || coachAttaching || coachMode === nextMode) return;
    setCoachAttaching(true);
    setCoachAttachError(null);
    try {
      if (nextMode === 'off') {
        await api.post(`/api/calls/${callSid}/coach/detach`);
        setCoachMode('off');
      } else {
        // attach is idempotent — backend detaches any existing sidecar and
        // re-attaches with the new mode, so we can switch on_request↔auto
        // without an explicit detach step here.
        await api.post(`/api/calls/${callSid}/coach/attach`, { mode: nextMode });
        setCoachMode(nextMode);
        setCoachOpen(true);  // confirm visually that it's live
      }
    } catch (err: any) {
      logger.error('[Coach] mode change failed:', err.response?.data || err);
      setCoachAttachError(
        err.response?.data?.error || 'Failed to change coach mode'
      );
    } finally {
      setCoachAttaching(false);
    }
  };

  const askCoach = async () => {
    if (!coachAskInput.trim() || !callSid) return;
    setCoachAsking(true);
    setCoachAskError(null);
    const question = coachAskInput.trim();
    // Optimistically render the agent's question in the feed so they see
    // it land immediately — the sidecar's async answer arrives via the
    // socket listener above and appears below it.
    setCoachSuggestions((prev) => [
      ...prev,
      {
        id: `agent-ask-${Date.now()}`,
        kind: 'agent_ask',
        text: question,
        timestamp: Date.now(),
      },
    ]);
    try {
      await api.post(`/api/calls/${callSid}/coach/ask`, { question });
      setCoachAskInput('');
    } catch (err: any) {
      logger.error('[Coach] Ask failed:', err.response?.data || err);
      setCoachAskError(err.response?.data?.error || 'Ask failed');
    } finally {
      setCoachAsking(false);
    }
  };

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

  const searchFactbook = async () => {
    if (!factbookQuery.trim() || !callSid) return;
    setFactbookLoading(true);
    setFactbookError(null);
    setFactbookNote(null);
    setFactbookSearched(true);
    try {
      // TODO: auto-derive collection_name from user's queue → AgentCollectionAssignment (M3-final / M5)
      const resp = await api.post(`/api/calls/${callSid}/kb-search`, {
        query: factbookQuery.trim(),
        collection_name: 'sales_knowledge',
        top_k: 5,
      });
      setFactbookResults(resp.data.results || []);
    } catch (err: any) {
      logger.error('[Factbook] Search failed:', err.response?.data || err);
      setFactbookError(err.response?.data?.error || 'Search failed');
      setFactbookResults([]);
    } finally {
      setFactbookLoading(false);
    }
  };

  const searchFactbookFromTranscript = async () => {
    if (!callSid) return;
    setFactbookLoading(true);
    setFactbookError(null);
    setFactbookNote(null);
    setFactbookSearched(true);
    try {
      const resp = await api.post(`/api/calls/${callSid}/kb-search-from-transcript`, {
        collection_name: 'sales_knowledge',
        n_utterances: 3,
        top_k: 5,
      });
      // Populate the input so the agent can see what was searched and edit/re-run.
      setFactbookQuery(resp.data.query || '');
      setFactbookResults(resp.data.results || []);
      if (resp.data.note) {
        setFactbookNote(resp.data.note);
      }
    } catch (err: any) {
      logger.error('[Factbook] Transcript search failed:', err.response?.data || err);
      setFactbookError(err.response?.data?.error || 'Search failed');
      setFactbookResults([]);
    } finally {
      setFactbookLoading(false);
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

      {/* Knowledge Factbook — only when the agent is actively connected to a
          caller, human-to-human. Pre-accept ("assigned to you" but not taken),
          ringing outbound, ended, or AI-active calls all hide it because the
          agent isn't talking to the caller yet (or won't be at all). */}
      {callSid && factbookEnabled && !isAICall && callState === 'active' && (
        <div className="border-t border-rule bg-canvas-sunken">
          <button
            onClick={() => setFactbookOpen(!factbookOpen)}
            className="w-full flex items-center justify-between px-5 py-3 hover:bg-canvas-sunken/80 transition-colors"
          >
            <div className="flex items-center gap-2">
              <BookOpen className="w-3.5 h-3.5 text-info-soft" />
              <span className="kicker text-info-soft">Knowledge factbook</span>
              <span className="text-[11.5px] text-ink-dim ml-1">
                Look up facts from the knowledge base.
              </span>
            </div>
            <ChevronDown className={`w-4 h-4 text-ink-faint transition-transform ${factbookOpen ? 'rotate-180' : ''}`} />
          </button>

          {factbookOpen && (
            <div className="px-5 pb-4">
              <div className="flex gap-2">
                <input
                  type="text"
                  value={factbookQuery}
                  onChange={(e) => setFactbookQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') searchFactbook(); }}
                  placeholder="What do you need to know? (e.g. 'return policy')"
                  className="input flex-1"
                  disabled={factbookLoading}
                />
                <button
                  onClick={searchFactbook}
                  disabled={!factbookQuery.trim() || factbookLoading}
                  className="btn-secondary disabled:opacity-50"
                >
                  {factbookLoading ? (
                    <div className="animate-spin rounded-full h-3.5 w-3.5 border-2 border-info-soft border-t-transparent" />
                  ) : (
                    <Search className="w-3.5 h-3.5" />
                  )}
                  Search
                </button>
                <button
                  onClick={searchFactbookFromTranscript}
                  disabled={factbookLoading || !callSid}
                  className="btn-secondary disabled:opacity-50"
                  title="Use the last 5 things the caller said as the search query"
                >
                  <MessageSquare className="w-3.5 h-3.5" />
                  From transcript
                </button>
              </div>

              {factbookError && (
                <div className="mt-2 p-2 bg-urgent/10 border border-urgent/30 rounded text-[11.5px] text-urgent-soft flex items-center gap-2">
                  <AlertCircle className="w-3 h-3 flex-shrink-0" />
                  {factbookError}
                </div>
              )}

              {factbookNote && !factbookError && (
                <div className="mt-2 p-2 bg-info/10 border border-info/30 rounded text-[11.5px] text-info-soft flex items-center gap-2">
                  <AlertCircle className="w-3 h-3 flex-shrink-0" />
                  {factbookNote}
                </div>
              )}

              {factbookResults.length > 0 && (
                <div className="mt-3 space-y-2 max-h-[280px] overflow-y-auto">
                  {factbookResults.map((result, idx) => (
                    <div key={idx} className="bg-canvas border border-rule rounded-md p-3 animate-fade-up">
                      <div className="flex items-center justify-between mb-2">
                        <span className="kicker text-info-soft">{result.filename || result.section || 'Untitled'}</span>
                        <span className="mono text-[10px] text-ink-faint">
                          {(result.score * 100).toFixed(0)}% match
                        </span>
                      </div>
                      <p className="text-[13px] leading-relaxed text-ink">{result.content}</p>
                    </div>
                  ))}
                </div>
              )}

              {factbookSearched && factbookResults.length === 0 && !factbookLoading && !factbookError && (
                <div className="mt-3 text-center text-[12px] text-ink-dim py-4">
                  No matching facts in the knowledge base for that query.
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* AI Coach (sidecar) — sibling to the Factbook above. Visibility is
          the `can_use_coach` permission (admin ceiling). Mode is an in-call
          agent decision: three buttons in the panel header attach/detach
          the sidecar on the fly. State machine:
            - 'off'        → no sidecar; mode picker shown
            - 'on_request' → sidecar attached, Ask input visible
            - 'auto'       → sidecar attached, no input (suggestions stream) */}
      {callSid && coachAllowed && !isAICall && callState === 'active' && (
        <div className="border-t border-rule bg-canvas-sunken">
          <div className="w-full flex items-center justify-between px-5 py-3">
            <button
              onClick={() => setCoachOpen(!coachOpen)}
              className="flex items-center gap-2 hover:opacity-80 transition-opacity"
            >
              <Sparkles className={`w-3.5 h-3.5 ${coachAttached ? 'text-ai-soft' : 'text-ink-faint'}`} />
              <span className={`kicker ${coachAttached ? 'text-ai-soft' : 'text-ink-dim'}`}>
                AI coach
              </span>
              {coachSuggestions.length > 0 && (
                <span className="chip chip-muted ml-1">
                  {coachSuggestions.length}
                </span>
              )}
              <ChevronDown
                className={`w-4 h-4 text-ink-faint transition-transform ${
                  coachOpen ? 'rotate-180' : ''
                }`}
              />
            </button>

            {/* In-call mode picker. Three buttons — clicking the current mode
                is a no-op; clicking another fires attach/detach. Active mode
                has the `ai` accent; inactive uses muted styling. */}
            <div className="flex items-center gap-1 rounded-md border border-rule bg-canvas p-0.5">
              {(['off', 'on_request', 'auto'] as const).map((m) => {
                const isActive = coachMode === m;
                const label = m === 'off' ? 'Off' : m === 'on_request' ? 'On request' : 'Auto';
                return (
                  <button
                    key={m}
                    onClick={() => changeCoachMode(m)}
                    disabled={coachAttaching}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded transition-colors disabled:opacity-50 ${
                      isActive
                        ? 'bg-ai/15 text-ai-soft'
                        : 'text-ink-dim hover:text-ink hover:bg-canvas-sunken'
                    }`}
                    title={
                      m === 'off'
                        ? 'No sidecar attached.'
                        : m === 'on_request'
                        ? 'Sidecar attached, stays silent — use "Ask coach" to pull suggestions.'
                        : 'Sidecar suggests on every customer turn.'
                    }
                  >
                    {coachAttaching && isActive ? '…' : label}
                  </button>
                );
              })}
            </div>
          </div>

          {coachAttachError && (
            <div className="px-5 pb-2 -mt-1">
              <div className="p-2 bg-urgent/10 border border-urgent/30 rounded text-[11.5px] text-urgent-soft flex items-center gap-2">
                <AlertCircle className="w-3 h-3 flex-shrink-0" />
                {coachAttachError}
              </div>
            </div>
          )}

          {coachOpen && (
            <div className="px-5 pb-4">
              {/* on_request input row. In auto mode we hide it because the
                  sidecar emits unprompted; an input box would be misleading.
                  When mode is 'off' there's nothing to ask, so we replace
                  the input with a nudge to pick a mode. */}
              {coachMode === 'on_request' && (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={coachAskInput}
                    onChange={(e) => setCoachAskInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') askCoach();
                    }}
                    placeholder="Ask the coach… (e.g. 'how should I handle this objection?')"
                    className="input flex-1"
                    disabled={coachAsking}
                  />
                  <button
                    onClick={askCoach}
                    disabled={!coachAskInput.trim() || coachAsking}
                    className="btn-secondary !border-ai/30 !text-ai-soft hover:!bg-ai/10 disabled:opacity-50"
                  >
                    {coachAsking ? (
                      <div className="animate-spin rounded-full h-3.5 w-3.5 border-2 border-ai-soft border-t-transparent" />
                    ) : (
                      <HelpCircle className="w-3.5 h-3.5" />
                    )}
                    Ask coach
                  </button>
                </div>
              )}

              {coachAskError && (
                <div className="mt-2 p-2 bg-urgent/10 border border-urgent/30 rounded text-[11.5px] text-urgent-soft flex items-center gap-2">
                  <AlertCircle className="w-3 h-3 flex-shrink-0" />
                  {coachAskError}
                </div>
              )}

              {/* Suggestion feed. agent_ask entries (local optimistic) render
                  on the right; everything else is a sidecar message on the
                  left. Mirrors a chat transcript so the flow reads as a
                  conversation between agent and coach. */}
              {coachSuggestions.length > 0 ? (
                <div
                  ref={coachFeedRef}
                  className="mt-3 space-y-2 max-h-[280px] overflow-y-auto"
                >
                  {coachSuggestions.map((s) => {
                    const isAgentAsk = s.kind === 'agent_ask';
                    return (
                      <div
                        key={s.id}
                        className={`flex animate-fade-up ${
                          isAgentAsk ? 'justify-end' : 'justify-start'
                        }`}
                      >
                        <div
                          className={`max-w-[85%] rounded-md p-3 border ${
                            isAgentAsk
                              ? 'bg-canvas border-rule text-ink'
                              : 'bg-ai/5 border-ai/25 text-ink'
                          }`}
                        >
                          <div className="flex items-center justify-between gap-3 mb-1">
                            <span
                              className={`kicker ${
                                isAgentAsk ? 'text-ink-dim' : 'text-ai-soft'
                              }`}
                            >
                              {isAgentAsk ? 'You asked' : 'Coach'}
                            </span>
                            <span className="mono text-[10px] text-ink-faint">
                              {new Date(s.timestamp).toLocaleTimeString([], {
                                hour: '2-digit',
                                minute: '2-digit',
                                second: '2-digit',
                              })}
                            </span>
                          </div>
                          <p className="text-[13px] leading-relaxed">{s.text}</p>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="mt-3 text-center text-[12px] text-ink-dim py-4">
                  {coachMode === 'off'
                    ? 'Pick "On request" or "Auto" above to attach the coach.'
                    : coachMode === 'auto'
                    ? 'Waiting for the next customer turn — suggestions will appear here.'
                    : 'Type a question above to get a coaching suggestion.'}
                </div>
              )}
            </div>
          )}
        </div>
      )}

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
