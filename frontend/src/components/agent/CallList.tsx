import React, { useState } from 'react';
import { Search, Clock, Bot, AlertCircle } from 'lucide-react';
import { cn, formatDuration, formatPhoneNumber } from '../../lib/utils';
import type { Call } from '../../types/callcenter';

interface CallListProps {
  calls: Call[];
  selectedCall: Call | null;
  filters: {
    waiting: boolean;
    aiActive: boolean;
    myCalls: boolean;
    completed: boolean;
  };
  onFilterChange: (filters: any) => void;
  onCallSelect: (call: Call) => void;
  onTakeCall: (call: Call) => void;
}

export const CallList: React.FC<CallListProps> = ({
  calls,
  selectedCall,
  filters,
  onFilterChange,
  onCallSelect,
  onTakeCall
}) => {
  const [searchTerm, setSearchTerm] = useState('');

  // Filter and categorize calls
  const categorizedCalls = {
    priority: calls.filter(c => c.priority === 'urgent' && c.status === 'waiting'),
    waiting: calls.filter(c => c.status === 'waiting' && c.priority !== 'urgent'),
    aiActive: calls.filter(c => c.status === 'ai_active'),
    myActive: calls.filter(c => c.status === 'active' && c.assignedTo === 'current_user'),
    completed: calls.filter(c => c.status === 'completed' || c.status === 'ended')
  };

  const filteredCalls = calls.filter(call => {
    if (searchTerm) {
      const search = searchTerm.toLowerCase();
      return (
        call.phoneNumber.includes(search) ||
        call.customerName?.toLowerCase().includes(search) ||
        call.queueId.toLowerCase().includes(search)
      );
    }
    return true;
  });

  return (
    <div className="h-full flex flex-col">
      {/* Search Bar */}
      <div className="p-6">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
          <input
            type="text"
            placeholder="Search by phone, name, or queue..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full pl-11 pr-4 py-3 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
      </div>

      {/* Call List */}
      <div className="flex-1 overflow-y-auto px-6 pb-6">
        {/* Priority Calls */}
        {categorizedCalls.priority.length > 0 && filters.waiting && (
          <CallSection
            title="⚡ Priority"
            calls={categorizedCalls.priority}
            selectedCall={selectedCall}
            onSelect={onCallSelect}
            onTakeCall={onTakeCall}
          />
        )}

        {/* Waiting Calls */}
        {categorizedCalls.waiting.length > 0 && filters.waiting && (
          <CallSection
            title="📋 Waiting"
            calls={categorizedCalls.waiting}
            selectedCall={selectedCall}
            onSelect={onCallSelect}
            onTakeCall={onTakeCall}
          />
        )}

        {/* AI Active Calls */}
        {categorizedCalls.aiActive.length > 0 && filters.aiActive && (
          <CallSection
            title="🤖 AI Active"
            calls={categorizedCalls.aiActive}
            selectedCall={selectedCall}
            onSelect={onCallSelect}
            onTakeCall={onTakeCall}
          />
        )}

        {/* My Active Calls */}
        {categorizedCalls.myActive.length > 0 && filters.myCalls && (
          <CallSection
            title="📞 Active"
            calls={categorizedCalls.myActive}
            selectedCall={selectedCall}
            onSelect={onCallSelect}
          />
        )}

        {/* Completed Calls */}
        {categorizedCalls.completed.length > 0 && filters.completed && (
          <CallSection
            title="✅ Completed"
            calls={categorizedCalls.completed}
            selectedCall={selectedCall}
            onSelect={onCallSelect}
          />
        )}

        {/* Empty State */}
        {filteredCalls.length === 0 && (
          <div className="flex items-center justify-center h-64 text-gray-400">
            <div className="text-center">
              <p className="text-sm">No calls found</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

const CallSection: React.FC<{
  title: string;
  calls: Call[];
  selectedCall: Call | null;
  onSelect: (call: Call) => void;
  onTakeCall?: (call: Call) => void;
}> = ({ title, calls, selectedCall, onSelect, onTakeCall }) => (
  <div className="border-b border-gray-100">
    <div className="px-4 py-2 bg-gray-50 text-xs font-semibold text-gray-600">
      {title} ({calls.length})
    </div>
    <div>
      {calls.map(call => (
        <CallCard
          key={call.id}
          call={call}
          isSelected={selectedCall?.id === call.id}
          onSelect={() => onSelect(call)}
          onTakeCall={onTakeCall}
        />
      ))}
    </div>
  </div>
);

const CallCard: React.FC<{
  call: Call;
  isSelected: boolean;
  onSelect: () => void;
  onTakeCall?: (call: Call) => void;
}> = ({ call, isSelected, onSelect, onTakeCall }) => {
  const getSentimentEmoji = (sentiment?: number) => {
    if (!sentiment) return '😐';
    if (sentiment > 0.5) return '😊';
    if (sentiment < -0.5) return '😟';
    return '😐';
  };

  const getWaitTime = () => {
    if (!call.startTime) return '';
    const start = new Date(call.startTime).getTime();
    const now = Date.now();
    const diff = Math.floor((now - start) / 1000);
    return formatDuration(diff * 1000);
  };

  return (
    <div
      onClick={onSelect}
      className={cn(
        "p-3 border-b border-gray-100 cursor-pointer transition-all",
        isSelected ? "bg-blue-50 border-l-4 border-l-blue-500" : "hover:bg-gray-50"
      )}
    >
      <div className="flex items-start justify-between mb-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center space-x-2 mb-1">
            <span className="font-medium text-sm text-gray-900 truncate">
              {formatPhoneNumber(call.phoneNumber)}
            </span>
            {call.status === 'ai_active' && (
              <Bot className="w-3 h-3 text-purple-500 flex-shrink-0" />
            )}
          </div>
          <div className="text-xs text-gray-600">
            {call.queueId} • {call.status === 'waiting' ? 'Waiting' : call.status}
          </div>
        </div>
        <div className="text-right ml-2">
          <div className="text-xs text-gray-500 flex items-center justify-end space-x-1">
            <Clock className="w-3 h-3" />
            <span>{getWaitTime()}</span>
          </div>
          <div className="text-lg">{getSentimentEmoji(call.sentiment)}</div>
        </div>
      </div>

      {/* Context */}
      {call.aiSummary && (
        <div className="text-xs text-gray-600 mb-2 truncate">
          "{call.aiSummary}"
        </div>
      )}

      {/* Priority Badge */}
      {call.priority === 'urgent' && (
        <div className="flex items-center space-x-1 text-xs text-red-600 mb-2">
          <AlertCircle className="w-3 h-3" />
          <span className="font-medium">URGENT</span>
        </div>
      )}

      {/* Take Call Button */}
      {call.status === 'waiting' && onTakeCall && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onTakeCall(call);
          }}
          className="w-full mt-2 px-3 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
        >
          Take Call
        </button>
      )}
    </div>
  );
};
