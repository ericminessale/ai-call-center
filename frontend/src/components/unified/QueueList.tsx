import { useState, useEffect, useMemo } from 'react';
import {
  Search,
  Phone,
  Clock,
  AlertTriangle,
  Building2,
  Star,
  Bot,
  UserCheck,
  Loader2,
} from 'lucide-react';
import { Call, QueueConfig } from '../../types/callcenter';
import { QueueItemSkeleton } from '../shared/Skeleton';
import { AgentContextCard, hasContext } from '../shared/AgentContextCard';
import { getQueueBadgeColor, getQueueDisplayName } from '../../lib/queueColors';

interface QueueListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
  onTakeCall: (call: Call) => void;
  isLoading?: boolean;
  queueConfigs?: QueueConfig[];
}

export function QueueList({ calls, onSelectCall, onTakeCall, isLoading, queueConfigs }: QueueListProps) {
  const [searchQuery, setSearchQuery] = useState('');
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
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      call.from_number?.toLowerCase().includes(query) ||
      call.contact?.displayName?.toLowerCase().includes(query) ||
      call.contact?.company?.toLowerCase().includes(query) ||
      call.queue_id?.toLowerCase().includes(query)
    );
  });

  const sortedCalls = [...filteredCalls].sort((a, b) => {
    const aNeedsAttention = a.is_urgent || (a.sentiment !== undefined && a.sentiment < -0.3);
    const bNeedsAttention = b.is_urgent || (b.sentiment !== undefined && b.sentiment < -0.3);
    if (aNeedsAttention && !bNeedsAttention) return -1;
    if (!aNeedsAttention && bNeedsAttention) return 1;
    return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
  });

  const isUrgentOrNegative = (c: Call) => c.is_urgent || c.queue_status === 'urgent' || (c.sentiment !== undefined && c.sentiment < -0.3);
  const urgentCalls = sortedCalls.filter(isUrgentOrNegative);
  const waitingCalls = sortedCalls.filter((c) => !isUrgentOrNegative(c) && c.status === 'waiting');
  const assignedCalls = sortedCalls.filter((c) => !isUrgentOrNegative(c) && c.status === 'assigned');

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-rule">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="kicker">Queue</span>
            {calls.length > 0 && (
              <span className="dot dot-wait" />
            )}
          </div>
          <span className="mono text-[11px] text-ink-dim">{calls.length}</span>
        </div>

        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-dim" />
          <input
            type="text"
            placeholder="Search queue…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="input pl-8 py-[7px]"
          />
        </div>

        {queuePills.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-3">
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
          <div>{Array.from({ length: 3 }).map((_, i) => <QueueItemSkeleton key={i} />)}</div>
        ) : sortedCalls.length === 0 ? (
          <EmptyQueue />
        ) : (
          <>
            {urgentCalls.length > 0 && (
              <QueueSection title="Urgent" icon={<AlertTriangle className="w-3 h-3" />} tone="urgent" count={urgentCalls.length}>
                {urgentCalls.map((call) => (
                  <QueueCard key={call.id} call={call} onSelect={() => onSelectCall(call)} onTake={() => onTakeCall(call)} queueConfigs={queueConfigs} />
                ))}
              </QueueSection>
            )}
            {waitingCalls.length > 0 && (
              <QueueSection title="Waiting" icon={<Loader2 className="w-3 h-3 animate-spin" />} tone="wait" count={waitingCalls.length}>
                {waitingCalls.map((call) => (
                  <QueueCard key={call.id} call={call} onSelect={() => onSelectCall(call)} onTake={() => onTakeCall(call)} queueConfigs={queueConfigs} />
                ))}
              </QueueSection>
            )}
            {assignedCalls.length > 0 && (
              <QueueSection title="Assigned" icon={<UserCheck className="w-3 h-3" />} tone="info" count={assignedCalls.length}>
                {assignedCalls.map((call) => (
                  <QueueCard key={call.id} call={call} onSelect={() => onSelectCall(call)} onTake={() => onTakeCall(call)} queueConfigs={queueConfigs} />
                ))}
              </QueueSection>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyQueue() {
  return (
    <div className="p-8 text-center">
      <Phone className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
      <p className="font-display text-[20px] text-ink-muted mb-1">Queue is clear</p>
      <p className="text-[12px] text-ink-dim">Callers waiting will show here, urgent first.</p>
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

function QueueSection({
  title,
  icon,
  tone,
  count,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  tone: 'urgent' | 'wait' | 'info';
  count: number;
  children: React.ReactNode;
}) {
  const colorClass = tone === 'urgent' ? 'text-urgent-soft' : tone === 'wait' ? 'text-wait-soft' : 'text-info-soft';
  return (
    <div>
      <div className="sticky top-0 z-10 bg-canvas-sunken/95 backdrop-blur-sm flex items-center justify-between px-4 py-1.5 border-b border-rule">
        <div className={`flex items-center gap-2 ${colorClass}`}>
          {icon}
          <span className="kicker" style={{ color: 'inherit' }}>{title}</span>
        </div>
        <span className="mono text-[10px] text-ink-dim">{count}</span>
      </div>
      {children}
    </div>
  );
}

function QueueCard({
  call,
  onSelect,
  onTake,
  queueConfigs,
}: {
  call: Call;
  onSelect: () => void;
  onTake: () => void;
  queueConfigs?: QueueConfig[];
}) {
  const contactName = call.contact?.displayName || call.from_number || 'Unknown caller';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;
  const wasAI = call.handler_type === 'ai' || call.ai_agent_name;
  const queueSlug = call.queue_id || '';
  const isNegativeSentiment = call.sentiment !== undefined && call.sentiment < -0.3;

  const queueBadge = queueSlug ? getQueueBadgeColor(queueSlug) : null;
  const queueDisplayName = queueSlug ? getQueueDisplayName(queueSlug, queueConfigs) : '';

  const [waitTime, setWaitTime] = useState('0:00');
  const [waitSeconds, setWaitSeconds] = useState(0);
  useEffect(() => {
    if (!call.created_at) return;
    const update = () => {
      const waitMs = Date.now() - new Date(call.created_at!).getTime();
      const totalSec = Math.floor(waitMs / 1000);
      const mins = Math.floor(totalSec / 60);
      const secs = totalSec % 60;
      setWaitTime(`${mins}:${String(secs).padStart(2, '0')}`);
      setWaitSeconds(totalSec);
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [call.created_at]);

  const railColor = (call.is_urgent || isNegativeSentiment)
    ? 'border-l-urgent'
    : wasAI ? 'border-l-ai' : 'border-l-wait';
  const waitToneClass = waitSeconds > 120 ? 'text-urgent-soft' : waitSeconds > 60 ? 'text-wait-soft' : 'text-ink';

  return (
    <div className={`px-4 py-3 border-b border-rule/60 border-l-[2px] ${railColor} hover:bg-canvas-hover/40`}>
      <div className="flex items-start gap-3">
        {/* Avatar */}
        <button
          onClick={onSelect}
          className={`shrink-0 w-9 h-9 rounded flex items-center justify-center text-[13px] font-semibold ${
            call.is_urgent ? 'bg-urgent/15 text-urgent-soft border border-urgent/30' :
            isVip ? 'bg-wait/15 text-wait-soft border border-wait/30' :
            'bg-canvas-raised text-ink-muted border border-rule'
          }`}
        >
          {contactName.charAt(0).toUpperCase()}
        </button>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <button onClick={onSelect} className="text-left w-full">
            <div className="flex items-center gap-1.5">
              <span className="font-medium text-ink truncate text-[13.5px]">{contactName}</span>
              {isVip && <Star className="w-3 h-3 text-wait fill-wait flex-shrink-0" />}
              {(call.is_urgent || isNegativeSentiment) && (
                <AlertTriangle className="w-3 h-3 text-urgent-soft flex-shrink-0" />
              )}
              {wasAI && (
                <span className="chip chip-ai"><Bot className="w-2.5 h-2.5" />AI</span>
              )}
            </div>
            <div className="flex items-center gap-1.5 text-[11.5px] text-ink-dim mt-0.5">
              {company && (
                <>
                  <Building2 className="w-3 h-3" />
                  <span className="truncate">{company}</span>
                  <span className="text-ink-faint">·</span>
                </>
              )}
              <Clock className="w-3 h-3" />
              <span className={`mono ${waitToneClass}`}>{waitTime}</span>
            </div>
          </button>

          {hasContext(call.aiContext) && (
            <AgentContextCard context={call.aiContext} variant="compact" className="mt-2" />
          )}

          <div className="mt-1.5 flex items-center gap-2 text-xs">
            {queueBadge && (
              <span className={`chip ${queueBadge.pill}`}>
                {queueDisplayName}
              </span>
            )}
            {call.status === 'assigned' && call.assigned_agent_id && (
              <span className="chip chip-info"><UserCheck className="w-2.5 h-2.5" />Assigned</span>
            )}
          </div>
        </div>

        {/* Take button */}
        <button
          onClick={(e) => { e.stopPropagation(); onTake(); }}
          className="btn-primary !py-1.5 !px-3 !text-[12px]"
        >
          <Phone className="w-3 h-3" />
          Take
        </button>
      </div>
    </div>
  );
}

export default QueueList;
