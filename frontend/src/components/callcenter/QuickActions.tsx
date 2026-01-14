import React from 'react';
import { motion } from 'framer-motion';
import { Phone, Coffee, CheckCircle, XCircle } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { AgentStatus } from '../../types/callcenter';

interface QuickActionsProps {
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  hasWaitingCalls: boolean;
  onTakeNextCall: () => void;
}

export const QuickActions: React.FC<QuickActionsProps> = ({
  agentStatus,
  onStatusChange,
  hasWaitingCalls,
  onTakeNextCall
}) => {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wide mb-3">
        Quick Actions
      </h3>
      <div className="grid grid-cols-2 gap-2">
        {/* Go Available */}
        <motion.button
          onClick={() => onStatusChange('available')}
          disabled={agentStatus === 'available' || agentStatus === 'busy'}
          className={cn(
            'flex items-center justify-center px-3 py-2 rounded-md text-sm font-medium transition-colors',
            agentStatus === 'available'
              ? 'bg-green-100 text-green-700 cursor-not-allowed'
              : 'bg-white text-gray-700 border border-gray-300 hover:bg-green-50'
          )}
          whileHover={{ scale: agentStatus !== 'available' ? 1.02 : 1 }}
          whileTap={{ scale: agentStatus !== 'available' ? 0.98 : 1 }}
        >
          <CheckCircle className="w-4 h-4 mr-1" />
          Available
        </motion.button>

        {/* Take Break */}
        <motion.button
          onClick={() => onStatusChange('break')}
          disabled={agentStatus === 'break' || agentStatus === 'busy'}
          className={cn(
            'flex items-center justify-center px-3 py-2 rounded-md text-sm font-medium transition-colors',
            agentStatus === 'break'
              ? 'bg-yellow-100 text-yellow-700 cursor-not-allowed'
              : 'bg-white text-gray-700 border border-gray-300 hover:bg-yellow-50'
          )}
          whileHover={{ scale: agentStatus !== 'break' ? 1.02 : 1 }}
          whileTap={{ scale: agentStatus !== 'break' ? 0.98 : 1 }}
        >
          <Coffee className="w-4 h-4 mr-1" />
          Break
        </motion.button>

        {/* Take Next Call */}
        {hasWaitingCalls && agentStatus === 'available' && (
          <motion.button
            onClick={onTakeNextCall}
            className="col-span-2 flex items-center justify-center px-3 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Phone className="w-4 h-4 mr-1" />
            Take Next Call
          </motion.button>
        )}

        {/* Go Offline */}
        <motion.button
          onClick={() => onStatusChange('offline')}
          disabled={agentStatus === 'offline' || agentStatus === 'busy'}
          className="col-span-2 flex items-center justify-center px-3 py-2 bg-white text-gray-700 border border-gray-300 rounded-md text-sm font-medium hover:bg-gray-50"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <XCircle className="w-4 h-4 mr-1" />
          Go Offline
        </motion.button>
      </div>
    </div>
  );
};