import { useState, useEffect, useMemo } from 'react';
import { Search, Phone, Bot, User, Building2, Star, AlertTriangle } from 'lucide-react';
import { Call, QueueConfig } from '../../types/callcenter';
import { logger } from '../../lib/logger';
import { CallListSkeletonGroup } from '../shared/Skeleton';
import { getQueueBadgeColor, getQueueDisplayName } from '../../lib/queueColors';
import ObserverControls from '../shared/ObserverControls';

interface ActiveCallsListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
  isLoading?: boolean;
  queueConfigs?: QueueConfig[];
}

type FilterType = 'all' | 'my-calls' | 'ai-active' | 'other';

export function ActiveCallsList({ calls, onSelectCall, isLoading, queueConfigs }: ActiveCallsListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');
  const [queueFilter, setQueueFilter] = useState<string | null>(null);

  const queuePills = useMemo(() => {
    if (!queueConfigs || queueConfigs.length === 0) return [];
    const counts: Record<string, number> = {};
    calls.forEach((c) => {
      const slug = c.queue_id || '';
      if (slug) counts[slug] = (counts[slug] || 0) + 1;
    });
    return queueConfigs.map((q) => ({
      slug: q.slug,
      label: q.display_name,
      count: counts[q.slug] || 0,
    }));
  }, [queueConfigs, calls]);

  const filteredCalls = calls.filter((call) => {
    if (queueFilter && (call.queue_id || '') !== queueFilter) return false;
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      const matches =
        call.from_number?.toLowerCase().includes(query) ||
        call.phoneNumber?.toLowerCase().includes(query) ||
        call.contact?.displayName?.toLowerCase().includes(query) ||
        call.contact?.company?.toLowerCase().includes(query);
      if (!matches) return false;
    }
    switch (activeFilter) {
      case 'my-calls':
        return call.handler_type === 'human' && (call.status === 'active' || call.status === 'connecting');
      case 'ai-active':
        return call.status === 'ai_active' || call.handler_type === 'ai';
      case 'other':
        return call.handler_type === 'human' && call.status !== 'active';
      default:
        return true;
    }
  });

  const myActiveCalls = filteredCalls.filter(
    (c) => c.handler_type === 'human' && (c.status === 'active' || c.status === 'connecting')
  );
  const aiCalls = filteredCalls.filter((c) => c.status === 'ai_active' || c.handler_type === 'ai');
  const otherCalls = filteredCalls.filter(
    (c) => c.handler_type === 'human' && c.status !== 'active' && c.status !== 'connecting'
  );

  const myCallIds = new Set(myActiveCalls.map(c => c.id));
  const aiCallIds = new Set(aiCalls.map(c => c.id));
  const otherCallIds = new Set(otherCalls.map(c => c.id));
  const uncategorizedCalls = filteredCalls.filter(
    (c) => !myCallIds.has(c.id) && !aiCallIds.has(c.id) && !otherCallIds.has(c.id)
  );

  if (uncategorizedCalls.length > 0) {
    logger.warn('[ActiveCallsList] Uncategorized calls:', uncategorizedCalls.map(c => ({
      id: c.id, status: c.status, handler_type: c.handler_type,
    })));
  }

  const filterButtons: { key: FilterType; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: calls.length },
    { key: 'my-calls', label: 'Mine', count: myActiveCalls.length },
    { key: 'ai-active', label: 'AI', count: aiCalls.length },
  ];

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-rule">
        <div className="flex items-center justify-between mb-3">
          <span className="kicker">Active calls</span>
          <span className="mono text-[11px] text-ink-dim">{calls.length}</span>
        </div>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-dim" />
          <input
            type="text"
            placeholder="Search calls…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="input pl-8 py-[7px]"
          />
        </div>

        {/* Filter tabs */}
        <div className="flex items-center gap-0 mt-3 p-0.5 bg-canvas-raised rounded border border-rule">
          {filterButtons.map((filter) => (
            <button
              key={filter.key}
              onClick={() => setActiveFilter(filter.key)}
              className={`flex-1 px-2 py-1 text-[11.5px] font-medium rounded transition-colors ${
                activeFilter === filter.key
                  ? 'bg-canvas-elevated text-ink border border-rule-strong'
                  : 'text-ink-muted hover:text-ink border border-transparent'
              }`}
            >
              {filter.label}
              {filter.count > 0 && (
                <span className="mono text-[10px] ml-1 opacity-80">{filter.count}</span>
              )}
            </button>
          ))}
        </div>

        {/* Queue pills */}
        {queuePills.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2.5">
            <QueuePill active={queueFilter === null} onClick={() => setQueueFilter(null)}>
              All queues
            </QueuePill>
            {queuePills.map((pill) => {
              const colors = getQueueBadgeColor(pill.slug);
              const isActive = queueFilter === pill.slug;
              return (
                <QueuePill
                  key={pill.slug}
                  active={isActive}
                  onClick={() => setQueueFilter(isActive ? null : pill.slug)}
                  tint={colors.dot}
                >
                  {pill.label}
                  {pill.count > 0 && (
                    <span className="mono text-[9.5px] ml-1 opacity-70">{pill.count}</span>
                  )}
                </QueuePill>
              );
            })}
          </div>
        )}
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <CallListSkeletonGroup count={3} />
        ) : filteredCalls.length === 0 ? (
          <EmptyState />
        ) : (
          <>
            {myActiveCalls.length > 0 && (
              <CallSection title="My calls" calls={myActiveCalls} onSelectCall={onSelectCall} tone="live" queueConfigs={queueConfigs} />
            )}
            {aiCalls.length > 0 && (
              <CallSection title="AI active" calls={aiCalls} onSelectCall={onSelectCall} tone="ai" queueConfigs={queueConfigs} />
            )}
            {otherCalls.length > 0 && (
              <CallSection title="Other agents" calls={otherCalls} onSelectCall={onSelectCall} tone="info" queueConfigs={queueConfigs} />
            )}
            {uncategorizedCalls.length > 0 && (
              <CallSection title="Other" calls={uncategorizedCalls} onSelectCall={onSelectCall} tone="wait" queueConfigs={queueConfigs} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="p-8 text-center">
      <Phone className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
      <p className="font-display text-[20px] text-ink-muted mb-1">All quiet</p>
      <p className="text-[12px] text-ink-dim">No calls in flight right now.</p>
    </div>
  );
}

function QueuePill({
  children,
  active,
  onClick,
  tint,
}: {
  children: React.ReactNode;
  active: boolean;
  onClick: () => void;
  tint?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm text-[10.5px] font-medium border transition-colors ${
        active
          ? 'bg-canvas-elevated text-ink border-rule-strong'
          : 'bg-canvas-raised text-ink-muted hover:text-ink border-rule hover:border-rule-strong'
      }`}
    >
      {!active && tint && (
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: tint }} />
      )}
      {children}
    </button>
  );
}

function CallSection({
  title,
  calls,
  onSelectCall,
  tone,
  queueConfigs,
}: {
  title: string;
  calls: Call[];
  onSelectCall: (call: Call) => void;
  tone: 'live' | 'ai' | 'info' | 'wait';
  queueConfigs?: QueueConfig[];
}) {
  const dotClass = tone === 'live' ? 'dot dot-live' : tone === 'ai' ? 'dot dot-ai' : tone === 'wait' ? 'dot dot-wait' : 'dot dot-offline';
  return (
    <div>
      <div className="sticky top-0 z-10 bg-canvas-sunken/95 backdrop-blur-sm flex items-center justify-between px-4 py-1.5 border-b border-rule">
        <div className="flex items-center gap-2">
          <span className={dotClass} />
          <span className="kicker">{title}</span>
        </div>
        <span className="mono text-[10px] text-ink-dim">{calls.length}</span>
      </div>
      {calls.map((call) => (
        <CallCard key={call.id} call={call} onClick={() => onSelectCall(call)} queueConfigs={queueConfigs} />
      ))}
    </div>
  );
}

function CallCard({ call, onClick, queueConfigs }: { call: Call; onClick: () => void; queueConfigs?: QueueConfig[] }) {
  const isAI = call.status === 'ai_active' || call.handler_type === 'ai';
  const isConnecting = call.status === 'connecting' || call.status === 'ringing';
  const contactName = call.contact?.displayName || call.from_number || 'Unknown';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;
  const queueSlug = call.queue_id || '';
  const isNegativeSentiment = call.sentiment !== undefined && call.sentiment < -0.3;

  const [liveDuration, setLiveDuration] = useState(call.duration || 0);
  useEffect(() => {
    if (isConnecting) return;
    setLiveDuration(call.duration || 0);
    const interval = setInterval(() => setLiveDuration(prev => prev + 1), 1000);
    return () => clearInterval(interval);
  }, [call.id, call.duration, isConnecting]);

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  const railColor = isNegativeSentiment ? 'border-l-urgent' : isAI ? 'border-l-ai' : isConnecting ? 'border-l-wait' : 'border-l-live';
  const queueDisplayName = queueSlug ? getQueueDisplayName(queueSlug, queueConfigs) : '';
  const queueBadge = queueSlug ? getQueueBadgeColor(queueSlug) : null;

  return (
    <button
      onClick={onClick}
      className={`relative w-full px-4 py-3 flex items-center gap-3 text-left border-b border-rule/60 border-l-[2px] ${railColor} transition-colors hover:bg-canvas-hover/40`}
    >
      {/* Avatar */}
      <div className="relative shrink-0">
        <div className={`w-9 h-9 rounded flex items-center justify-center text-[13px] font-semibold ${
          isAI ? 'bg-ai/15 text-ai-soft border border-ai/30' :
          isConnecting ? 'bg-wait/15 text-wait-soft border border-wait/30' :
          'bg-live/15 text-live-soft border border-live/30'
        }`}>
          {isAI ? <Bot className="w-4 h-4" /> : contactName.charAt(0).toUpperCase()}
        </div>
        {!isConnecting && (
          <span className={`absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full ${
            isAI ? 'bg-ai shadow-[0_0_6px_rgba(138,123,255,0.8)]' : 'bg-live shadow-[0_0_6px_rgba(63,183,126,0.8)]'
          }`} />
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-ink truncate text-[13.5px]">{contactName}</span>
          {isNegativeSentiment && (
            <AlertTriangle className="w-3 h-3 text-urgent-soft flex-shrink-0" />
          )}
          {isVip && (
            <Star className="w-3 h-3 text-wait fill-wait flex-shrink-0" />
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[11.5px] text-ink-dim mt-0.5 min-w-0">
          {company ? (
            <>
              <Building2 className="w-3 h-3 flex-shrink-0" />
              <span className="truncate">{company}</span>
            </>
          ) : call.from_number ? (
            <span className="mono truncate">{call.from_number}</span>
          ) : null}
          {queueBadge && (company || call.from_number) && <span className="text-ink-faint">·</span>}
          {queueBadge && (
            <span className={`chip ${queueBadge.pill} !border-0 !px-1 !py-0 text-[9.5px]`}>
              {queueDisplayName}
            </span>
          )}
        </div>
      </div>

      {/* Status + duration */}
      <div className="flex flex-col items-end gap-1 shrink-0">
        {isAI ? (
          <span className="chip chip-ai"><Bot className="w-2.5 h-2.5" />AI</span>
        ) : isConnecting ? (
          <span className="chip chip-wait">Connect…</span>
        ) : (
          <span className="chip chip-live">Live</span>
        )}
        <span className="mono text-[11.5px] text-ink-muted">
          {isConnecting ? '—:—' : formatDuration(liveDuration)}
        </span>
        {/* Observer action — surfaces if (a) the viewer has the right listen
            permission (agents get null in ObserverControls itself) AND
            (b) the call's transport supports monitor. Bridge-mode calls
            lack monitor_listen until promote-to-conference (M4); showing
            the button on them would 5xx the click. Stops click propagation
            so clicking it doesn't also select the row. */}
        {(!Array.isArray(call.capabilities)
          || call.capabilities.includes('monitor_listen')) && (
          <div onClick={(e) => e.stopPropagation()}>
            <ObserverControls
              callId={call.id}
              callType={isAI ? 'ai' : 'human'}
              compact
            />
          </div>
        )}
      </div>
    </button>
  );
}

export default ActiveCallsList;
