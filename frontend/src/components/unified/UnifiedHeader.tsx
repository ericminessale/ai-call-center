import { useState } from 'react';
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
} from 'lucide-react';
import { ViewMode, AgentStatus } from '../../pages/UnifiedAgentDesktop';
import { QuickDialDropdown } from './QuickDialDropdown';

interface UnifiedHeaderProps {
  user: { email: string } | null;
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  stats: {
    callsToday: number;
    avgHandleTime: number;
    fcr: number;
    csat: number;
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

const statusConfig: Record<AgentStatus, { label: string; color: string; bgColor: string }> = {
  available: { label: 'Available', color: 'text-green-400', bgColor: 'bg-green-500' },
  busy: { label: 'Busy', color: 'text-red-400', bgColor: 'bg-red-500' },
  'after-call': { label: 'After Call', color: 'text-yellow-400', bgColor: 'bg-yellow-500' },
  break: { label: 'Break', color: 'text-orange-400', bgColor: 'bg-orange-500' },
  offline: { label: 'Offline', color: 'text-gray-400', bgColor: 'bg-gray-500' },
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
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showQuickDial, setShowQuickDial] = useState(false);

  const currentStatus = statusConfig[agentStatus];

  return (
    <header className="bg-gray-800 border-b border-gray-700">
      {/* Top row - Logo, Status, Stats, User */}
      <div className="h-14 flex items-center justify-between px-4">
        {/* Left - Logo and Title */}
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold text-white">SignalWire Call Center</h1>
        </div>

        {/* Center - Agent Status and Stats */}
        <div className="flex items-center gap-6">
          {/* Agent Status Dropdown */}
          <div className="relative">
            <button
              onClick={() => setShowStatusDropdown(!showStatusDropdown)}
              className="flex items-center gap-2 px-3 py-1.5 bg-gray-700 rounded-lg hover:bg-gray-600 transition-colors"
            >
              <Circle className={`w-2.5 h-2.5 fill-current ${currentStatus.color}`} />
              <span className={`text-sm font-medium ${currentStatus.color}`}>
                {currentStatus.label}
              </span>
              <ChevronDown className="w-4 h-4 text-gray-400" />
            </button>

            {showStatusDropdown && (
              <div className="absolute top-full left-0 mt-1 w-40 bg-gray-700 rounded-lg shadow-lg border border-gray-600 py-1 z-50">
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
              <div className="text-gray-400 text-xs">FCR</div>
              <div className="text-white font-medium">{stats.fcr}%</div>
            </div>
            <div className="text-center">
              <div className="text-gray-400 text-xs">CSAT</div>
              <div className="text-white font-medium">{stats.csat.toFixed(1)}</div>
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
                  onClick={() => setShowUserMenu(false)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left text-gray-300 hover:bg-gray-600"
                >
                  <Settings className="w-4 h-4" />
                  <span className="text-sm">Settings</span>
                </button>
                <hr className="border-gray-600 my-1" />
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
