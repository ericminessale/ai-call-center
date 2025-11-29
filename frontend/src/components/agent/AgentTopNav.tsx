import React from 'react';
import { Bell, Phone, ChevronDown, Settings, LogOut } from 'lucide-react';
import { cn } from '../../lib/utils';
import { useAuthStore } from '../../stores/authStore';
import type { AgentStatus } from '../../types/callcenter';

interface AgentTopNavProps {
  agentName: string;
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  stats: {
    callsToday: number;
    avgHandleTime: number;
    fcr: number;
    csat: number;
  };
  onPhoneToggle: () => void;
  isPhoneExpanded: boolean;
}

const statusColors: Record<AgentStatus, string> = {
  available: 'bg-green-500',
  busy: 'bg-red-500',
  'after-call': 'bg-orange-500',
  break: 'bg-yellow-500',
  offline: 'bg-gray-500'
};

const statusLabels: Record<AgentStatus, string> = {
  available: 'Available',
  busy: 'On Call',
  'after-call': 'After Call Work',
  break: 'On Break',
  offline: 'Offline'
};

export const AgentTopNav: React.FC<AgentTopNavProps> = ({
  agentName,
  agentStatus,
  onStatusChange,
  stats,
  onPhoneToggle,
  isPhoneExpanded
}) => {
  const { logout } = useAuthStore();
  const [showStatusMenu, setShowStatusMenu] = React.useState(false);
  const [showNotifications, setShowNotifications] = React.useState(false);
  const [showSettings, setShowSettings] = React.useState(false);
  const [showClearDbConfirm, setShowClearDbConfirm] = React.useState(false);

  return (
    <div className="bg-white border-b border-gray-200 px-6 py-3">
      <div className="flex items-center justify-between">
        {/* Left: Agent Status */}
        <div className="flex items-center space-x-4">
          <div className="relative">
            <button
              onClick={() => setShowStatusMenu(!showStatusMenu)}
              className="flex items-center space-x-2 px-4 py-2 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <div className={cn(
                "w-3 h-3 rounded-full",
                statusColors[agentStatus],
                agentStatus === 'busy' && "animate-pulse"
              )} />
              <span className="font-medium text-gray-900">
                {statusLabels[agentStatus]}
              </span>
              <ChevronDown className="w-4 h-4 text-gray-500" />
            </button>

            {/* Status Dropdown */}
            {showStatusMenu && (
              <div className="absolute top-full left-0 mt-2 w-48 bg-white rounded-lg shadow-lg border border-gray-200 py-1 z-50">
                {(Object.keys(statusLabels) as AgentStatus[]).map(status => (
                  <button
                    key={status}
                    onClick={() => {
                      onStatusChange(status);
                      setShowStatusMenu(false);
                    }}
                    className="w-full flex items-center space-x-2 px-4 py-2 hover:bg-gray-50 transition-colors"
                  >
                    <div className={cn("w-2 h-2 rounded-full", statusColors[status])} />
                    <span className="text-sm text-gray-700">{statusLabels[status]}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="text-sm text-gray-600">
            {agentName}
          </div>
        </div>

        {/* Center: Stats */}
        <div className="flex items-center space-x-6">
          <StatBadge label="Calls Today" value={stats.callsToday} />
          <StatBadge label="Avg Handle Time" value={formatTime(stats.avgHandleTime)} />
          <StatBadge label="FCR" value={`${stats.fcr}%`} />
          <StatBadge label="CSAT" value={stats.csat.toFixed(1)} suffix="/5" />
        </div>

        {/* Right: Actions */}
        <div className="flex items-center space-x-3">
          {/* Notifications */}
          <div className="relative">
            <button
              onClick={() => setShowNotifications(!showNotifications)}
              className="p-2 rounded-lg hover:bg-gray-100 transition-colors relative"
            >
              <Bell className="w-5 h-5 text-gray-600" />
              <span className="absolute top-1 right-1 w-2 h-2 bg-red-500 rounded-full" />
            </button>

            {showNotifications && (
              <div className="absolute top-full right-0 mt-2 w-80 bg-white rounded-lg shadow-lg border border-gray-200 py-2 z-50">
                <div className="px-4 py-2 border-b border-gray-100">
                  <h3 className="font-semibold text-sm">Notifications</h3>
                </div>
                <div className="px-4 py-8 text-center text-gray-500 text-sm">
                  No new notifications
                </div>
              </div>
            )}
          </div>

          {/* Settings */}
          <div className="relative">
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="p-2 rounded-lg hover:bg-gray-100 transition-colors"
            >
              <Settings className="w-5 h-5 text-gray-600" />
            </button>

            {showSettings && (
              <div className="absolute top-full right-0 mt-2 w-80 bg-white rounded-lg shadow-lg border border-gray-200 py-2 z-50">
                <div className="px-4 py-2 border-b border-gray-100">
                  <h3 className="font-semibold text-sm">Settings</h3>
                </div>
                <div className="px-4 py-3 space-y-3">
                  {/* Logout Button */}
                  <button
                    onClick={() => {
                      logout();
                      setShowSettings(false);
                    }}
                    className="w-full flex items-center justify-center space-x-2 px-4 py-2 bg-gray-600 text-white text-sm font-medium rounded-lg hover:bg-gray-700 transition-colors"
                  >
                    <LogOut className="w-4 h-4" />
                    <span>Log Out</span>
                  </button>

                  {/* Clear Database Button */}
                  <button
                    onClick={() => {
                      setShowClearDbConfirm(true);
                      setShowSettings(false);
                    }}
                    className="w-full px-4 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700 transition-colors"
                  >
                    Clear Database
                  </button>
                  <p className="mt-2 text-xs text-gray-500">
                    Remove all stale calls from the database
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Phone Widget Toggle */}
          <button
            onClick={onPhoneToggle}
            className={cn(
              "flex items-center space-x-2 px-4 py-2 rounded-lg transition-colors",
              isPhoneExpanded ? "bg-blue-100 text-blue-700" : "bg-gray-100 text-gray-700 hover:bg-gray-200"
            )}
          >
            <Phone className="w-4 h-4" />
            <span className="font-medium text-sm">Phone</span>
            <ChevronDown className={cn(
              "w-4 h-4 transition-transform",
              isPhoneExpanded && "rotate-180"
            )} />
          </button>
        </div>
      </div>

      {/* Clear Database Confirmation Modal */}
      {showClearDbConfirm && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-xl p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold text-gray-900 mb-2">
              Clear Database
            </h3>
            <p className="text-sm text-gray-600 mb-6">
              Are you sure you want to clear all stale calls from the database? This action cannot be undone.
            </p>
            <div className="flex space-x-3 justify-end">
              <button
                onClick={() => setShowClearDbConfirm(false)}
                className="px-4 py-2 bg-gray-200 text-gray-700 text-sm font-medium rounded-lg hover:bg-gray-300 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  try {
                    const token = localStorage.getItem('access_token');
                    const response = await fetch('/api/admin/clear-calls', {
                      method: 'POST',
                      headers: { 'Authorization': `Bearer ${token}` }
                    });

                    if (response.ok) {
                      alert('Database cleared successfully');
                      window.location.reload();
                    } else {
                      alert('Failed to clear database');
                    }
                  } catch (error) {
                    console.error('Error clearing database:', error);
                    alert('Error clearing database');
                  }
                  setShowClearDbConfirm(false);
                }}
                className="px-4 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700 transition-colors"
              >
                Yes, Clear Database
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

const StatBadge: React.FC<{ label: string; value: string | number; suffix?: string }> = ({
  label,
  value,
  suffix
}) => (
  <div className="text-center">
    <div className="text-xs text-gray-500 mb-1">{label}</div>
    <div className="font-semibold text-gray-900">
      {value}{suffix}
    </div>
  </div>
);

function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}
