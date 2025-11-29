import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Settings, Bell, HelpCircle, LogOut, FlaskConical } from 'lucide-react';
import { StatusSelector } from './StatusSelector';
import { formatTime } from '../../lib/utils';
import type { AgentStatus } from '../../types/callcenter';

interface GlobalNavProps {
  agentStatus: AgentStatus;
  agentName: string;
  onStatusChange: (status: AgentStatus) => void;
  statusStartTime: Date;
  onGenerateMockData?: () => void;
}

export const GlobalNav: React.FC<GlobalNavProps> = ({
  agentStatus,
  agentName,
  onStatusChange,
  statusStartTime,
  onGenerateMockData
}) => {
  const [statusDuration, setStatusDuration] = useState(0);

  // Update status timer every second
  useEffect(() => {
    const interval = setInterval(() => {
      const duration = Math.floor((Date.now() - statusStartTime.getTime()) / 1000);
      setStatusDuration(duration);
    }, 1000);

    return () => clearInterval(interval);
  }, [statusStartTime]);

  return (
    <motion.header
      className="bg-white border-b border-gray-200 px-6 py-3"
      initial={{ y: -20, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
    >
      <div className="flex items-center justify-between">
        {/* Left Section - Logo and Brand */}
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-2">
            <div className="w-8 h-8 bg-gradient-to-r from-blue-600 to-indigo-600 rounded-lg flex items-center justify-center">
              <span className="text-white font-bold text-sm">SW</span>
            </div>
            <span className="font-semibold text-gray-900">SignalWire</span>
            <span className="text-gray-500 text-sm">Call Center</span>
          </div>
        </div>

        {/* Center Section - Agent Info and Status */}
        <div className="flex items-center space-x-6">
          <div className="flex items-center space-x-3">
            <div className="flex items-center space-x-2">
              <div className="w-8 h-8 bg-gray-200 rounded-full flex items-center justify-center">
                <span className="text-gray-600 font-medium text-sm">
                  {agentName.charAt(0).toUpperCase()}
                </span>
              </div>
              <span className="text-sm font-medium text-gray-900">{agentName}</span>
            </div>

            <div className="h-6 w-px bg-gray-300" />

            <StatusSelector
              currentStatus={agentStatus}
              onStatusChange={onStatusChange}
              statusDuration={statusDuration}
            />

            {agentStatus === 'break' && statusDuration > 0 && (
              <div className="text-sm text-gray-600">
                Break: {formatTime(statusDuration)}
              </div>
            )}
          </div>
        </div>

        {/* Right Section - Actions */}
        <div className="flex items-center space-x-3">
          {onGenerateMockData && (
            <button
              onClick={onGenerateMockData}
              className="flex items-center space-x-2 px-3 py-1.5 bg-gradient-to-r from-purple-500 to-pink-500 text-white rounded-lg hover:from-purple-600 hover:to-pink-600 transition-all"
              title="Generate demo data"
            >
              <FlaskConical className="w-4 h-4" />
              <span className="text-sm font-medium">Demo Data</span>
            </button>
          )}

          <button className="p-2 hover:bg-gray-100 rounded-lg transition-colors relative">
            <Bell className="w-5 h-5 text-gray-600" />
            <span className="absolute top-1 right-1 w-2 h-2 bg-red-500 rounded-full" />
          </button>

          <button className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
            <HelpCircle className="w-5 h-5 text-gray-600" />
          </button>

          <button className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
            <Settings className="w-5 h-5 text-gray-600" />
          </button>

          <div className="h-6 w-px bg-gray-300 mx-2" />

          <button className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-gray-600">
            <LogOut className="w-5 h-5" />
          </button>
        </div>
      </div>
    </motion.header>
  );
};