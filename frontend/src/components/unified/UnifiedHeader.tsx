import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Users,
  Phone,
  ListTodo,
  Eye,
  LogOut,
  ChevronDown,
  PhoneCall,
  Settings,
  Circle,
  AlertTriangle,
} from 'lucide-react';
import { ViewMode, AgentStatus } from '../../pages/UnifiedAgentDesktop';
import Logo from '../shared/Logo';
import { QuickDialDropdown } from './QuickDialDropdown';
import { queueApi } from '../../services/api';
import toast from 'react-hot-toast';

interface UnifiedHeaderProps {
  user: { email: string } | null;
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  stats: {
    callsToday: number;
    avgHandleTime: number;
    queueDepth: number;
    longestWait: number;
  };
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  callCounts: {
    active: number;
    queue: number;
    aiActive: number;
  };
  onLogout: () => void;
  callFabric: any;
  onOutboundCallStarted?: (phoneNumber: string) => void;
}

interface AvailableQueue {
  id: number;
  slug: string;
  display_name: string;
  routing_strategy: string;
  assignment_id: number | null;
  is_assigned: boolean;
  is_activated: boolean;
}

const statusConfig: Record<AgentStatus, { label: string; color: string; bgColor: string }> = {
  available: { label: 'Available', color: 'text-green-400', bgColor: 'bg-green-500' },
  busy: { label: 'Busy', color: 'text-red-400', bgColor: 'bg-red-500' },
  'after-call': { label: 'After Call', color: 'text-yellow-400', bgColor: 'bg-yellow-500' },
  break: { label: 'Break', color: 'text-orange-400', bgColor: 'bg-orange-500' },
  offline: { label: 'Offline', color: 'text-gray-400', bgColor: 'bg-gray-500' },
};

const STRATEGY_SHORT: Record<string, string> = {
  fifo: 'FIFO',
  round_robin: 'RR',
  priority: 'Priority',
  skill_based: 'Skill',
};

export function UnifiedHeader({
  user,
  agentStatus,
  onStatusChange,
  stats,
  viewMode,
  onViewModeChange,
  callCounts,
  onLogout,
  callFabric,
  onOutboundCallStarted,
}: UnifiedHeaderProps) {
  const navigate = useNavigate();
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showQuickDial, setShowQuickDial] = useState(false);

  // Queue opt-in state
  const [availableQueues, setAvailableQueues] = useState<AvailableQueue[]>([]);

  const loadQueues = useCallback(async () => {
    try {
      const resp = await queueApi.getAvailableQueues();
      setAvailableQueues(resp.data.queues);
    } catch {
      setAvailableQueues([]);
    }
  }, []);

  useEffect(() => { loadQueues(); }, [loadQueues]);

  const handleQueueToggle = async (queue: AvailableQueue) => {
    try {
      if (!queue.is_assigned) {
        // Self-subscribe: create assignment + activate
        const resp = await queueApi.selfSubscribe(queue.id);
        setAvailableQueues(prev => prev.map(q =>
          q.id === queue.id
            ? { ...q, assignment_id: resp.data.assignment.id, is_assigned: true, is_activated: true }
            : q
        ));
      } else {
        // Toggle existing assignment
        const resp = await queueApi.toggleQueueActivation(queue.assignment_id!, !queue.is_activated);
        setAvailableQueues(prev => prev.map(q =>
          q.id === queue.id ? { ...q, is_activated: resp.data.assignment.is_activated } : q
        ));
      }
    } catch {
      toast.error('Failed to update queue');
    }
  };

  const currentStatus = statusConfig[agentStatus];
  const activeQueueCount = availableQueues.filter(q => q.is_activated).length;

  return (
    <header className="bg-gray-800 border-b border-gray-700">
      {/* Top row - Logo, Status, Stats, User */}
      <div className="h-14 flex items-center justify-between px-4">
        {/* Left - Logo and Title */}
        <div className="flex items-center gap-3">
          <Logo size="sm" />
          <h1 className="text-lg font-semibold text-white">SignalWire Call Center</h1>
        </div>

        {/* Center - Agent Status and Stats */}
        <div className="flex items-center gap-6">
          {/* Agent Status Dropdown */}
          <div className="relative flex items-center gap-2">
            <button
              onClick={() => setShowStatusDropdown(!showStatusDropdown)}
              className="flex items-center gap-2 px-3 py-1.5 bg-gray-700 rounded-lg hover:bg-gray-600 transition-colors"
            >
              <Circle className={`w-2.5 h-2.5 fill-current ${currentStatus.color}`} />
              <span className={`text-sm font-medium ${currentStatus.color}`}>
                {currentStatus.label}
              </span>
              {activeQueueCount > 0 && agentStatus !== 'offline' && (
                <span className="px-1.5 py-0.5 text-[9px] rounded-full bg-green-600 text-white leading-none">
                  {activeQueueCount}
                </span>
              )}
              <ChevronDown className="w-4 h-4 text-gray-400" />
            </button>

            {/* Conference Error Indicator */}
            {callFabric.conferenceJoinError && (
              <div
                className="flex items-center gap-1 px-2 py-1 bg-red-900/50 border border-red-700 rounded-lg cursor-help"
                title={callFabric.conferenceJoinError}
              >
                <AlertTriangle className="w-4 h-4 text-red-400" />
                <span className="text-xs text-red-400">Conference Error</span>
              </div>
            )}

            {/* Connecting indicator */}
            {callFabric.isChangingStatus && (
              <div className="flex items-center gap-1 px-2 py-1 bg-blue-900/50 border border-blue-700 rounded-lg">
                <div className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
                <span className="text-xs text-blue-400">Connecting...</span>
              </div>
            )}

            {showStatusDropdown && (
              <div className="absolute top-full left-0 mt-1 w-56 bg-gray-700 rounded-lg shadow-lg border border-gray-600 py-1 z-50">
                {/* Status options */}
                {(Object.keys(statusConfig) as AgentStatus[]).map((status) => (
                  <button
                    key={status}
                    onClick={() => {
                      onStatusChange(status);
                      setShowStatusDropdown(false);
                    }}
                    className={`w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-600 ${
                      status === agentStatus ? 'bg-gray-600' : ''
                    }`}
                  >
                    <Circle className={`w-2.5 h-2.5 fill-current ${statusConfig[status].color}`} />
                    <span className={`text-sm ${statusConfig[status].color}`}>
                      {statusConfig[status].label}
                    </span>
                  </button>
                ))}

                {/* Queue opt-in section */}
                {availableQueues.length > 0 && (
                  <>
                    <div className="border-t border-gray-600 my-1" />
                    <div className="px-3 py-1.5">
                      <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">My Queues</span>
                    </div>
                    <div className="max-h-40 overflow-y-auto">
                      {availableQueues.map(queue => (
                        <label
                          key={queue.id}
                          className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-600 cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            checked={queue.is_activated}
                            onChange={() => handleQueueToggle(queue)}
                            className="rounded bg-gray-600 border-gray-500 text-blue-500 focus:ring-blue-500 focus:ring-offset-0"
                          />
                          <span className="text-sm text-gray-200 flex-1">{queue.display_name}</span>
                          <span className="px-1.5 py-0.5 text-[9px] rounded bg-blue-900/40 text-blue-300">
                            {STRATEGY_SHORT[queue.routing_strategy] || queue.routing_strategy}
                          </span>
                        </label>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>

          {/* Stats */}
          <div className="flex items-center gap-4 text-sm">
            <div className="text-center">
              <div className="text-gray-400 text-xs">Today</div>
              <div className="text-white font-medium">{stats.callsToday}</div>
            </div>
            <div className="text-center">
              <div className="text-gray-400 text-xs">Avg Time</div>
              <div className="text-white font-medium">
                {Math.floor(stats.avgHandleTime / 60)}:{String(stats.avgHandleTime % 60).padStart(2, '0')}
              </div>
            </div>
            <div className="text-center">
              <div className="text-gray-400 text-xs">In Queue</div>
              <div className={`font-medium ${stats.queueDepth > 0 ? 'text-yellow-400' : 'text-white'}`}>
                {stats.queueDepth}
              </div>
            </div>
            <div className="text-center">
              <div className="text-gray-400 text-xs">Wait</div>
              <div className={`font-medium ${stats.longestWait > 60 ? 'text-red-400' : stats.longestWait > 30 ? 'text-yellow-400' : 'text-white'}`}>
                {stats.longestWait > 0
                  ? `${Math.floor(stats.longestWait / 60)}:${String(stats.longestWait % 60).padStart(2, '0')}`
                  : '—'
                }
              </div>
            </div>
          </div>
        </div>

        {/* Right - Quick Dial, User Menu */}
        <div className="flex items-center gap-3">
          {/* Quick Dial Button */}
          <div className="relative">
            <button
              onClick={() => setShowQuickDial(!showQuickDial)}
              className={`p-2 rounded-lg transition-colors ${
                callFabric.isOnline
                  ? 'text-green-400 hover:bg-gray-700'
                  : 'text-gray-400 hover:bg-gray-700'
              }`}
              title={callFabric.isOnline ? 'Quick Dial (Online)' : 'Quick Dial (Offline)'}
            >
              <PhoneCall className="w-5 h-5" />
            </button>

            {showQuickDial && (
              <QuickDialDropdown
                callFabric={callFabric}
                onClose={() => setShowQuickDial(false)}
                onCallStarted={onOutboundCallStarted}
              />
            )}
          </div>

          {/* User Menu */}
          <div className="relative">
            <button
              onClick={() => setShowUserMenu(!showUserMenu)}
              className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-700 rounded-lg transition-colors"
            >
              <span className="text-sm text-gray-300">{user?.email}</span>
              <ChevronDown className="w-4 h-4 text-gray-400" />
            </button>

            {showUserMenu && (
              <div className="absolute top-full right-0 mt-1 w-48 bg-gray-700 rounded-lg shadow-lg border border-gray-600 py-1 z-50">
                <button
                  onClick={() => {
                    setShowUserMenu(false);
                    onLogout();
                  }}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left text-red-400 hover:bg-gray-600"
                >
                  <LogOut className="w-4 h-4" />
                  <span className="text-sm">Logout</span>
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Bottom row - View Tabs */}
      <div className="h-10 flex items-center px-4 border-t border-gray-700/50">
        <nav className="flex items-center gap-1">
          <ViewTab
            icon={<Users className="w-4 h-4" />}
            label="Contacts"
            active={viewMode === 'contacts'}
            onClick={() => onViewModeChange('contacts')}
          />
          <ViewTab
            icon={<Phone className="w-4 h-4" />}
            label="Active Calls"
            count={callCounts.active}
            active={viewMode === 'calls'}
            onClick={() => onViewModeChange('calls')}
          />
          <ViewTab
            icon={<ListTodo className="w-4 h-4" />}
            label="Queue"
            count={callCounts.queue}
            active={viewMode === 'queue'}
            onClick={() => onViewModeChange('queue')}
          />
          <ViewTab
            icon={<Eye className="w-4 h-4" />}
            label="Supervisor"
            active={viewMode === 'supervisor'}
            onClick={() => onViewModeChange('supervisor')}
          />
          <ViewTab
            icon={<Settings className="w-4 h-4" />}
            label="Settings"
            active={viewMode === 'settings'}
            onClick={() => onViewModeChange('settings')}
          />
        </nav>
      </div>
    </header>
  );
}

function ViewTab({
  icon,
  label,
  count,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-3 py-1.5 rounded-md text-sm transition-colors ${
        active
          ? 'bg-blue-600 text-white'
          : 'text-gray-400 hover:text-white hover:bg-gray-700'
      }`}
    >
      {icon}
      <span>{label}</span>
      {count !== undefined && count > 0 && (
        <span
          className={`px-1.5 py-0.5 text-xs rounded-full ${
            active ? 'bg-blue-500 text-white' : 'bg-gray-600 text-gray-300'
          }`}
        >
          {count}
        </span>
      )}
    </button>
  );
}

export default UnifiedHeader;
