import { useState } from 'react';
import {
  Search,
  Phone,
  Clock,
  AlertTriangle,
  Building2,
  Star,
  PhoneIncoming,
  Bot,
} from 'lucide-react';
import { Call } from '../../types/callcenter';

interface QueueListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
  onTakeCall: (call: Call) => void;
}

export function QueueList({ calls, onSelectCall, onTakeCall }: QueueListProps) {
  const [searchQuery, setSearchQuery] = useState('');

  // Filter calls based on search
  const filteredCalls = calls.filter((call) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      call.from_number?.toLowerCase().includes(query) ||
      call.contact?.displayName?.toLowerCase().includes(query) ||
      call.contact?.company?.toLowerCase().includes(query) ||
      call.queue_id?.toLowerCase().includes(query)
    );
  });

  // Sort by priority (urgent first) then wait time
  const sortedCalls = [...filteredCalls].sort((a, b) => {
    if (a.is_urgent && !b.is_urgent) return -1;
    if (!a.is_urgent && b.is_urgent) return 1;
    // Sort by wait time (older first)
    return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
  });

  // Separate urgent and regular
  const urgentCalls = sortedCalls.filter((c) => c.is_urgent);
  const regularCalls = sortedCalls.filter((c) => !c.is_urgent);

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-3 border-b border-gray-700">
        <h2 className="text-sm font-semibold text-white mb-2 flex items-center gap-2">
          <PhoneIncoming className="w-4 h-4 text-yellow-400" />
          Waiting Calls ({calls.length})
        </h2>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search queue..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500"
          />
        </div>
      </div>

      {/* Queue List */}
      <div className="flex-1 overflow-y-auto">
        {sortedCalls.length === 0 ? (
          <div className="p-4 text-center text-gray-400">
            <Phone className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No calls in queue</p>
            <p className="text-xs mt-1">
              Calls will appear here when customers are waiting
            </p>
          </div>
        ) : (
          <>
            {/* Urgent/Priority Calls */}
            {urgentCalls.length > 0 && (
              <div className="mb-2">
                <div className="px-3 py-2 text-xs font-semibold text-red-400 uppercase tracking-wider bg-red-900/20 flex items-center gap-2">
                  <AlertTriangle className="w-3 h-3" />
                  Priority ({urgentCalls.length})
                </div>
                {urgentCalls.map((call) => (
                  <QueueCard
                    key={call.id}
                    call={call}
                    onSelect={() => onSelectCall(call)}
                    onTake={() => onTakeCall(call)}
                  />
                ))}
              </div>
            )}

            {/* Regular Queue */}
            {regularCalls.length > 0 && (
              <div>
                <div className="px-3 py-2 text-xs font-semibold text-gray-400 uppercase tracking-wider bg-gray-800/50">
                  Queue ({regularCalls.length})
                </div>
                {regularCalls.map((call) => (
                  <QueueCard
                    key={call.id}
                    call={call}
                    onSelect={() => onSelectCall(call)}
                    onTake={() => onTakeCall(call)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function QueueCard({
  call,
  onSelect,
  onTake,
}: {
  call: Call;
  onSelect: () => void;
  onTake: () => void;
}) {
  const contactName = call.contact?.displayName || call.from_number || 'Unknown Caller';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;
  const wasAI = call.handler_type === 'ai' || call.ai_agent_name;

  // Calculate wait time
  const getWaitTime = () => {
    if (!call.created_at) return '0:00';
    const waitMs = Date.now() - new Date(call.created_at).getTime();
    const mins = Math.floor(waitMs / 60000);
    const secs = Math.floor((waitMs % 60000) / 1000);
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  return (
    <div className="px-3 py-3 border-b border-gray-700/50 hover:bg-gray-700/30">
      <div className="flex items-start gap-3">
        {/* Avatar */}
        <button
          onClick={onSelect}
          className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-medium flex-shrink-0 ${
            call.is_urgent ? 'bg-red-600' : isVip ? 'bg-yellow-600' : 'bg-gray-600'
          }`}
        >
          {contactName.charAt(0).toUpperCase()}
        </button>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <button onClick={onSelect} className="text-left w-full">
            <div className="flex items-center gap-2">
              <span className="font-medium text-white truncate">{contactName}</span>
              {isVip && (
                <Star className="w-3 h-3 text-yellow-400 fill-yellow-400 flex-shrink-0" />
              )}
              {call.is_urgent && (
                <AlertTriangle className="w-3 h-3 text-red-400 flex-shrink-0" />
              )}
            </div>
            <div className="flex items-center gap-2 text-xs text-gray-400 mt-0.5">
              {company && (
                <>
                  <Building2 className="w-3 h-3" />
                  <span className="truncate">{company}</span>
                  <span>•</span>
                </>
              )}
              <Clock className="w-3 h-3" />
              <span>Waiting {getWaitTime()}</span>
            </div>
          </button>

          {/* AI Context (if escalated from AI) */}
          {wasAI && call.ai_summary && (
            <div className="mt-2 p-2 bg-purple-900/20 border border-purple-700/50 rounded text-xs">
              <div className="flex items-center gap-1 text-purple-400 mb-1">
                <Bot className="w-3 h-3" />
                <span className="font-medium">AI Context</span>
              </div>
              <p className="text-gray-300 line-clamp-2">{call.ai_summary}</p>
            </div>
          )}

          {/* Queue info */}
          {call.queue_id && (
            <div className="mt-1 text-xs text-gray-500">
              Queue: {call.queue_id}
            </div>
          )}
        </div>

        {/* Take Call Button */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onTake();
          }}
          className="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white text-xs font-medium rounded-lg transition-colors flex items-center gap-1"
        >
          <Phone className="w-3 h-3" />
          Take
        </button>
      </div>
    </div>
  );
}

export default QueueList;
