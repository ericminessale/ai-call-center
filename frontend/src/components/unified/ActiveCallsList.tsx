import { useState } from 'react';
import { Search, Phone, Bot, User, Clock, Building2, Star } from 'lucide-react';
import { Call } from '../../types/callcenter';

interface ActiveCallsListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
}

type FilterType = 'all' | 'my-calls' | 'ai-active' | 'other';

export function ActiveCallsList({ calls, onSelectCall }: ActiveCallsListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');

  // Debug logging
  console.log('📋 [ActiveCallsList] Rendering with calls:', calls.map(c => ({
    id: c.id,
    status: c.status,
    handler_type: c.handler_type,
    from_number: c.from_number,
    phoneNumber: c.phoneNumber
  })));

  // Filter calls based on search and filter type
  const filteredCalls = calls.filter((call) => {
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
    console.warn('⚠️ [ActiveCallsList] Uncategorized calls:', uncategorizedCalls.map(c => ({
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
      </div>

      {/* Call List */}
      <div className="flex-1 overflow-y-auto">
        {filteredCalls.length === 0 ? (
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
}: {
  title: string;
  calls: Call[];
  onSelectCall: (call: Call) => void;
  icon: React.ReactNode;
  color: string;
}) {
  return (
    <div className="mb-2">
      <div className={`px-3 py-2 text-xs font-semibold uppercase tracking-wider bg-gray-800/50 flex items-center gap-2 ${color}`}>
        {icon}
        {title} ({calls.length})
      </div>
      {calls.map((call) => (
        <CallCard key={call.id} call={call} onClick={() => onSelectCall(call)} />
      ))}
    </div>
  );
}

function CallCard({ call, onClick }: { call: Call; onClick: () => void }) {
  const isAI = call.status === 'ai_active';
  const contactName = call.contact?.displayName || call.from_number || 'Unknown';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;

  // Format duration
  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  return (
    <button
      onClick={onClick}
      className="w-full px-3 py-3 flex items-center gap-3 text-left hover:bg-gray-700/50 border-l-2 border-transparent hover:border-blue-500 transition-colors"
    >
      {/* Avatar */}
      <div
        className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-medium ${
          isAI ? 'bg-purple-600' : 'bg-green-600'
        }`}
      >
        {isAI ? <Bot className="w-5 h-5" /> : contactName.charAt(0).toUpperCase()}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-white truncate">{contactName}</span>
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
        </div>
      </div>

      {/* Status */}
      <div className="flex flex-col items-end gap-1">
        <div className="flex items-center gap-1 text-xs">
          {isAI ? (
            <span className="text-purple-400">AI Agent</span>
          ) : (
            <span className="text-green-400">Active</span>
          )}
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-500">
          <Clock className="w-3 h-3" />
          <span>{formatDuration(call.duration || 0)}</span>
        </div>
      </div>
    </button>
  );
}

export default ActiveCallsList;
