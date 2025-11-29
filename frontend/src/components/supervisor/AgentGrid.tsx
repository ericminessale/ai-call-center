import React, { useState } from 'react';
import { Grid, List, Eye, MessageSquare, PhoneCall, Bot } from 'lucide-react';
import { cn, formatDuration } from '../../lib/utils';
import type { AgentWithCall } from '../../pages/SupervisorDashboard';

interface AgentGridProps {
  agents: AgentWithCall[];
  focusedAgent: AgentWithCall | null;
  viewMode: 'grid' | 'list';
  filterStatus: 'all' | 'active' | 'available' | 'busy';
  onViewModeChange: (mode: 'grid' | 'list') => void;
  onFilterChange: (filter: 'all' | 'active' | 'available' | 'busy') => void;
  onAgentSelect: (agent: AgentWithCall) => void;
  onMonitor: (agentId: string) => void;
  onWhisper: (agentId: string) => void;
  onBarge: (agentId: string) => void;
}

export const AgentGrid: React.FC<AgentGridProps> = ({
  agents,
  focusedAgent,
  viewMode,
  filterStatus,
  onViewModeChange,
  onFilterChange,
  onAgentSelect,
  onMonitor,
  onWhisper,
  onBarge
}) => {
  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h3 className="text-sm font-semibold text-gray-900">
          Agent Monitoring ({agents.length})
        </h3>

        <div className="flex items-center space-x-2">
          {/* View Mode Toggle */}
          <div className="flex items-center bg-gray-100 rounded-lg p-1">
            <button
              onClick={() => onViewModeChange('grid')}
              className={cn(
                "p-1.5 rounded transition-colors",
                viewMode === 'grid' ? "bg-white shadow-sm" : "hover:bg-gray-200"
              )}
            >
              <Grid className="w-4 h-4" />
            </button>
            <button
              onClick={() => onViewModeChange('list')}
              className={cn(
                "p-1.5 rounded transition-colors",
                viewMode === 'list' ? "bg-white shadow-sm" : "hover:bg-gray-200"
              )}
            >
              <List className="w-4 h-4" />
            </button>
          </div>

          {/* Filter Dropdown */}
          <select
            value={filterStatus}
            onChange={(e) => onFilterChange(e.target.value as any)}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="all">All Agents</option>
            <option value="active">Active Only</option>
            <option value="available">Available Only</option>
            <option value="busy">Busy Only</option>
          </select>
        </div>
      </div>

      {/* Agent Cards */}
      <div className="flex-1 overflow-y-auto p-4">
        {agents.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center text-gray-400">
              <svg className="w-16 h-16 mx-auto mb-4 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
              <p className="text-lg font-medium mb-2">No agents online</p>
              <p className="text-sm">Enable Demo Mode to see sample data</p>
            </div>
          </div>
        ) : viewMode === 'grid' ? (
          <div className="grid grid-cols-3 gap-4">
            {agents.map(agent => (
              <AgentCard
                key={agent.id}
                agent={agent}
                isFocused={focusedAgent?.id === agent.id}
                onSelect={() => onAgentSelect(agent)}
                onMonitor={() => onMonitor(agent.id)}
                onWhisper={() => onWhisper(agent.id)}
                onBarge={() => onBarge(agent.id)}
              />
            ))}
          </div>
        ) : (
          <div className="space-y-2">
            {agents.map(agent => (
              <AgentListItem
                key={agent.id}
                agent={agent}
                isFocused={focusedAgent?.id === agent.id}
                onSelect={() => onAgentSelect(agent)}
                onMonitor={() => onMonitor(agent.id)}
                onWhisper={() => onWhisper(agent.id)}
                onBarge={() => onBarge(agent.id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

const AgentCard: React.FC<{
  agent: AgentWithCall;
  isFocused: boolean;
  onSelect: () => void;
  onMonitor: () => void;
  onWhisper: () => void;
  onBarge: () => void;
}> = ({ agent, isFocused, onSelect, onMonitor, onWhisper, onBarge }) => {
  const [isHovered, setIsHovered] = useState(false);

  const getStatusColor = () => {
    if (agent.activeCall) {
      // Has active call - check sentiment
      if (agent.sentiment !== undefined) {
        if (agent.sentiment < -0.5) return 'bg-red-500'; // Negative
        if (agent.sentiment < 0) return 'bg-yellow-500'; // Neutral-negative
        return 'bg-green-500'; // Positive
      }
      return 'bg-green-500'; // Active but no sentiment
    }

    // No active call - check status
    switch (agent.status) {
      case 'available':
        return 'bg-green-500';
      case 'busy':
        return 'bg-red-500';
      case 'after-call':
        return 'bg-orange-500';
      case 'break':
        return 'bg-yellow-500';
      case 'offline':
        return 'bg-gray-400';
      default:
        return 'bg-gray-400';
    }
  };

  const getSentimentEmoji = () => {
    if (agent.sentiment === undefined) return null;
    if (agent.sentiment > 0.5) return '😊';
    if (agent.sentiment < -0.5) return '😟';
    return '😐';
  };

  const hasAlert = agent.sentiment !== undefined && agent.sentiment < -0.3;

  return (
    <div
      onClick={onSelect}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      className={cn(
        "relative bg-white border-2 rounded-lg p-4 cursor-pointer transition-all",
        isFocused ? "border-blue-500 shadow-lg" : "border-gray-200 hover:border-blue-300 hover:shadow-md",
        hasAlert && "border-red-300"
      )}
    >
      {/* Alert Badge */}
      {hasAlert && (
        <div className="absolute -top-2 -right-2 w-6 h-6 bg-red-500 rounded-full flex items-center justify-center">
          <span className="text-white text-xs font-bold">!</span>
        </div>
      )}

      {/* Agent Info */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center space-x-2 mb-1">
            <div className={cn("w-3 h-3 rounded-full animate-pulse", getStatusColor())} />
            <span className="text-sm font-semibold text-gray-900 truncate">
              {agent.name}
            </span>
            {agent.name.startsWith('AI-') && (
              <Bot className="w-4 h-4 text-purple-500 flex-shrink-0" />
            )}
          </div>
          <div className="text-xs text-gray-600">
            {agent.queues.join(', ')}
          </div>
        </div>
      </div>

      {/* Call Info */}
      {agent.activeCall ? (
        <div className="space-y-1 mb-3">
          <div className="text-sm font-medium text-gray-900">
            {formatDuration((agent.callDuration || 0) * 1000)}
          </div>
          {agent.sentiment !== undefined && (
            <div className="text-2xl">
              {getSentimentEmoji()} <span className="text-xs text-gray-600">
                {agent.sentiment.toFixed(2)}
              </span>
            </div>
          )}
        </div>
      ) : (
        <div className="text-xs text-gray-500 mb-3">
          {agent.status === 'available' ? 'Ready for calls' : agent.status}
        </div>
      )}

      {/* Hover Actions */}
      {isHovered && agent.activeCall && (
        <div className="absolute inset-0 bg-black/5 rounded-lg flex flex-col items-center justify-center space-y-2 p-2">
          <button
            onClick={(e) => { e.stopPropagation(); onMonitor(); }}
            className="w-full flex items-center justify-center space-x-2 px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors text-sm font-medium"
          >
            <Eye className="w-4 h-4" />
            <span>Monitor</span>
          </button>
          <div className="flex items-center space-x-2 w-full">
            <button
              onClick={(e) => { e.stopPropagation(); onWhisper(); }}
              className="flex-1 flex items-center justify-center space-x-1 px-2 py-1.5 bg-white border border-gray-300 rounded text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              <MessageSquare className="w-3.5 h-3.5" />
              <span>Whisper</span>
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onBarge(); }}
              className="flex-1 flex items-center justify-center space-x-1 px-2 py-1.5 bg-white border border-gray-300 rounded text-xs font-medium hover:bg-gray-50 transition-colors"
            >
              <PhoneCall className="w-3.5 h-3.5" />
              <span>Barge</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

const AgentListItem: React.FC<{
  agent: AgentWithCall;
  isFocused: boolean;
  onSelect: () => void;
  onMonitor: () => void;
  onWhisper: () => void;
  onBarge: () => void;
}> = ({ agent, isFocused, onSelect, onMonitor, onWhisper, onBarge }) => {
  const [isHovered, setIsHovered] = useState(false);

  const getStatusColor = () => {
    if (agent.activeCall) {
      if (agent.sentiment !== undefined && agent.sentiment < -0.5) return 'bg-red-500';
      return 'bg-green-500';
    }
    switch (agent.status) {
      case 'available': return 'bg-green-500';
      case 'busy': return 'bg-red-500';
      default: return 'bg-gray-400';
    }
  };

  return (
    <div
      onClick={onSelect}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      className={cn(
        "flex items-center justify-between p-3 bg-white border rounded-lg cursor-pointer transition-all",
        isFocused ? "border-blue-500 shadow-md" : "border-gray-200 hover:border-blue-300"
      )}
    >
      {/* Agent Info */}
      <div className="flex items-center space-x-3 flex-1">
        <div className={cn("w-3 h-3 rounded-full animate-pulse", getStatusColor())} />
        <div className="flex-1">
          <div className="flex items-center space-x-2">
            <span className="text-sm font-semibold text-gray-900">{agent.name}</span>
            {agent.name.startsWith('AI-') && <Bot className="w-4 h-4 text-purple-500" />}
          </div>
          <div className="text-xs text-gray-600">{agent.queues.join(', ')}</div>
        </div>
        {agent.activeCall && (
          <div className="text-sm font-medium text-gray-900">
            {formatDuration((agent.callDuration || 0) * 1000)}
          </div>
        )}
        {agent.sentiment !== undefined && (
          <div className="text-lg">{agent.sentiment > 0.5 ? '😊' : agent.sentiment < -0.5 ? '😟' : '😐'}</div>
        )}
      </div>

      {/* Hover Actions */}
      {isHovered && agent.activeCall && (
        <div className="flex items-center space-x-2 ml-4">
          <button
            onClick={(e) => { e.stopPropagation(); onMonitor(); }}
            className="p-2 bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors"
            title="Monitor"
          >
            <Eye className="w-4 h-4" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onWhisper(); }}
            className="p-2 bg-white border border-gray-300 rounded hover:bg-gray-50 transition-colors"
            title="Whisper"
          >
            <MessageSquare className="w-4 h-4" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onBarge(); }}
            className="p-2 bg-white border border-gray-300 rounded hover:bg-gray-50 transition-colors"
            title="Barge In"
          >
            <PhoneCall className="w-4 h-4" />
          </button>
        </div>
      )}
    </div>
  );
};
