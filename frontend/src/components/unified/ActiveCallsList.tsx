import { useState, useEffect, useMemo } from 'react';
import { Search, Phone, Bot, User, Clock, Building2, Star, Headphones, AlertTriangle } from 'lucide-react';
import { Call, QueueConfig } from '../../types/callcenter';
import { logger } from '../../lib/logger';
import { CallListSkeletonGroup } from '../shared/Skeleton';
import { getQueueBadgeColor, getQueueDisplayName } from '../../lib/queueColors';

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
  const [queueFilter, setQueueFilter] = useState<string | null>(null); // null = all queues

  // Build queue pills data from configs + actual calls
  const queuePills = useMemo(() => {
    if (!queueConfigs || queueConfigs.length === 0) return [];
    // Count calls per queue slug
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

  // Filter calls based on search, filter type, and queue filter
  const filteredCalls = calls.filter((call) => {
    // Queue filter
    if (queueFilter && (call.queue_id || '') !== queueFilter) return false;

    // Search filter
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      const matchesSearch =
        call.from_number?.toLowerCase().includes(query) ||
        call.phoneNumber?.toLowerCase().includes(query) ||
        call.contact?.displayName?.toLowerCase().includes(query) ||
        call.contact?.company?.toLowerCase().includes(query);
      if (!matchesSearch) return false;
    }

    // Type filter
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

  // Group calls by status - AI calls include handler_type === 'ai' or status === 'ai_active'
  const myActiveCalls = filteredCalls.filter(
    (c) => c.handler_type === 'human' && (c.status === 'active' || c.status === 'connecting')
  );
  const aiCalls = filteredCalls.filter((c) => c.status === 'ai_active' || c.handler_type === 'ai');
  const otherCalls = filteredCalls.filter(
    (c) => c.handler_type === 'human' && c.status !== 'active' && c.status !== 'connecting'
  );

  // Catch any calls that don't fit categories (shouldn't happen, but prevents invisible calls)
  const myCallIds = new Set(myActiveCalls.map(c => c.id));
  const aiCallIds = new Set(aiCalls.map(c => c.id));
  const otherCallIds = new Set(otherCalls.map(c => c.id));
  const uncategorizedCalls = filteredCalls.filter(
    (c) => !myCallIds.has(c.id) && !aiCallIds.has(c.id) && !otherCallIds.has(c.id)
  );

  if (uncategorizedCalls.length > 0) {
    logger.warn('[ActiveCallsList] Uncategorized calls:', uncategorizedCalls.map(c => ({
      id: c.id,
      status: c.status,
      handler_type: c.handler_type
    })));
  }

  const filterButtons: { key: FilterType; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: calls.length },
    { key: 'my-calls', label: 'My Calls', count: myActiveCalls.length },
    { key: 'ai-active', label: 'AI Active', count: aiCalls.length },
  ];

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-3 border-b border-gray-700">
        {/* Search */}
        <div className="relative mb-3">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search calls..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* Filter Buttons */}
        <div className="flex gap-1">
          {filterButtons.map((filter) => (
            <button
              key={filter.key}
              onClick={() => setActiveFilter(filter.key)}
              className={`flex-1 px-2 py-1.5 text-xs rounded-md transition-colors ${
                activeFilter === filter.key
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
              }`}
            >
              {filter.label}
              {filter.count > 0 && (
                <span className="ml-1 opacity-75">({filter.count})</span>
              )}
            </button>
          ))}
        </div>

        {/* Queue Filter Pills */}
        {queuePills.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            <button
              onClick={() => setQueueFilter(null)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-medium transition-colors ${
                queueFilter === null
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-700/60 text-gray-400 hover:bg-gray-600'
              }`}
            >
              All Queues
            </button>
            {queuePills.map((pill) => {
              const colors = getQueueBadgeColor(pill.slug);
              const isActive = queueFilter === pill.slug;
              return (
                <button
                  key={pill.slug}
                  onClick={() => setQueueFilter(isActive ? null : pill.slug)}
                  className={`px-2.5 py-1 rounded-full text-[11px] font-medium transition-colors ${
                    isActive
                      ? 'bg-blue-600 text-white'
                      : `${colors.pill} hover:brightness-125`
                  }`}
                >
                  {pill.label}
                  {pill.count > 0 && (
                    <span className="ml-1 opacity-75">({pill.count})</span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Call List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <CallListSkeletonGroup count={3} />
        ) : filteredCalls.length === 0 ? (
          <div className="p-4 text-center text-gray-400">
            <Phone className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No active calls</p>
          </div>
        ) : (
          <>
            {/* My Active Calls */}
            {myActiveCalls.length > 0 && (
              <CallSection
                title="My Calls"
                calls={myActiveCalls}
                onSelectCall={onSelectCall}
                icon={<User className="w-3 h-3" />}
                color="text-blue-400"
                queueConfigs={queueConfigs}
              />
            )}

            {/* AI Active Calls */}
            {aiCalls.length > 0 && (
              <CallSection
                title="AI Active"
                calls={aiCalls}
                onSelectCall={onSelectCall}
                icon={<Bot className="w-3 h-3" />}
                color="text-purple-400"
                queueConfigs={queueConfigs}
              />
            )}

            {/* Other Calls */}
            {otherCalls.length > 0 && (
              <CallSection
                title="Other Agents"
                calls={otherCalls}
                onSelectCall={onSelectCall}
                icon={<User className="w-3 h-3" />}
                color="text-gray-400"
                queueConfigs={queueConfigs}
              />
            )}

            {/* Uncategorized Calls (fallback to ensure all calls are visible) */}
            {uncategorizedCalls.length > 0 && (
              <CallSection
                title="Other"
                calls={uncategorizedCalls}
                onSelectCall={onSelectCall}
                icon={<Phone className="w-3 h-3" />}
                color="text-yellow-400"
                queueConfigs={queueConfigs}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function CallSection({
  title,
  calls,
  onSelectCall,
  icon,
  color,
  queueConfigs,
}: {
  title: string;
  calls: Call[];
  onSelectCall: (call: Call) => void;
  icon: React.ReactNode;
  color: string;
  queueConfigs?: QueueConfig[];
}) {
  return (
    <div className="mb-2">
      <div className={`px-3 py-2 text-xs font-semibold uppercase tracking-wider bg-gray-800/50 flex items-center gap-2 ${color}`}>
        {icon}
        {title} ({calls.length})
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

  // Live ticking duration
  const [liveDuration, setLiveDuration] = useState(call.duration || 0);
  useEffect(() => {
    if (isConnecting) return;
    setLiveDuration(call.duration || 0);
    const interval = setInterval(() => {
      setLiveDuration(prev => prev + 1);
    }, 1000);
    return () => clearInterval(interval);
  }, [call.id, call.duration, isConnecting]);

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  // Negative sentiment detection
  const isNegativeSentiment = call.sentiment !== undefined && call.sentiment < -0.3;

  // Determine border and background colors based on call type + sentiment
  const borderColor = isNegativeSentiment ? 'border-red-500' : isAI ? 'border-purple-500' : isConnecting ? 'border-yellow-500' : 'border-green-500';
  const bgTint = isNegativeSentiment ? 'bg-red-900/10' : isAI ? 'bg-purple-900/10' : '';

  // Queue badge
  const queueBadge = queueSlug ? getQueueBadgeColor(queueSlug) : null;
  const queueDisplayName = queueSlug ? getQueueDisplayName(queueSlug, queueConfigs) : '';

  return (
    <button
      onClick={onClick}
      className={`w-full px-3 py-3 flex items-center gap-3 text-left hover:bg-gray-700/50 border-l-2 ${borderColor} ${bgTint} transition-colors`}
    >
      {/* Avatar */}
      <div className="relative">
        <div
          className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-medium ${
            isAI ? 'bg-purple-600' : isConnecting ? 'bg-yellow-600' : 'bg-green-600'
          }`}
        >
          {isAI ? <Bot className="w-5 h-5" /> : contactName.charAt(0).toUpperCase()}
        </div>
        {/* Live pulse indicator */}
        {!isConnecting && (
          <span className={`absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-gray-800 ${
            isAI ? 'bg-purple-400' : 'bg-green-400'
          }`}>
            <span className={`absolute inset-0 rounded-full animate-ping opacity-75 ${
              isAI ? 'bg-purple-400' : 'bg-green-400'
            }`} />
          </span>
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-white truncate">{contactName}</span>
          {isNegativeSentiment && (
            <AlertTriangle className="w-3 h-3 text-red-400 flex-shrink-0" title="Negative sentiment detected" />
          )}
          {isVip && (
            <Star className="w-3 h-3 text-yellow-400 fill-yellow-400 flex-shrink-0" />
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-400">
          {company && (
            <>
              <Building2 className="w-3 h-3" />
              <span className="truncate">{company}</span>
            </>
          )}
          {!company && call.from_number && <span>{call.from_number}</span>}
          {/* Queue Badge */}
          {queueBadge && (
            <>
              {(company || call.from_number) && <span className="text-gray-600">·</span>}
              <span className={`px-1.5 py-0.5 text-[9px] font-semibold rounded ${queueBadge.pill}`}>
                {queueDisplayName}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Status Badge + Duration */}
      <div className="flex flex-col items-end gap-1.5">
        {isAI ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/30 uppercase tracking-wider">
            <Bot className="w-3 h-3" />
            AI
          </span>
        ) : isConnecting ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold rounded-full bg-yellow-500/20 text-yellow-300 border border-yellow-500/30 uppercase tracking-wider animate-pulse">
            Connecting
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold rounded-full bg-green-500/20 text-green-300 border border-green-500/30 uppercase tracking-wider">
            <Headphones className="w-3 h-3" />
            Live
          </span>
        )}
        <div className="flex items-center gap-1 text-xs text-gray-500 tabular-nums">
          <Clock className="w-3 h-3" />
          <span>{isConnecting ? '--:--' : formatDuration(liveDuration)}</span>
        </div>
      </div>
    </button>
  );
}

export default ActiveCallsList;
