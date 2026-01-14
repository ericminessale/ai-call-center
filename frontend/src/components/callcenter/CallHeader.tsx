import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Phone, MapPin, Clock, AlertTriangle } from 'lucide-react';
import { formatTime, formatPhoneNumber } from '../../lib/utils';
import type { Call } from '../../types/callcenter';

interface CallHeaderProps {
  call: Call;
}

export const CallHeader: React.FC<CallHeaderProps> = ({ call }) => {
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      const start = new Date(call.startTime).getTime();
      const now = Date.now();
      setDuration(Math.floor((now - start) / 1000));
    }, 1000);

    return () => clearInterval(interval);
  }, [call.startTime]);

  const priorityColors = {
    low: 'bg-gray-100 text-gray-700',
    medium: 'bg-blue-100 text-blue-700',
    high: 'bg-orange-100 text-orange-700',
    urgent: 'bg-red-100 text-red-700 animate-pulse'
  };

  return (
    <motion.div
      className="bg-white border-b border-gray-200 px-6 py-4"
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <div className="flex items-center justify-between">
        {/* Left Section - Caller Info */}
        <div className="flex items-center space-x-4">
          <div className="flex items-center">
            <div className="w-10 h-10 bg-green-100 rounded-full flex items-center justify-center">
              <Phone className="w-5 h-5 text-green-600 animate-pulse" />
            </div>
          </div>

          <div>
            <div className="flex items-center space-x-2">
              <h3 className="text-lg font-semibold text-gray-900">
                {call.customerName || 'Unknown Caller'}
              </h3>
              {call.priority && (
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  priorityColors[String(call.priority).toLowerCase() as keyof typeof priorityColors] || priorityColors.medium
                }`}>
                  {String(call.priority).toLowerCase() === 'urgent' && <AlertTriangle className="w-3 h-3 inline mr-1" />}
                  {String(call.priority).toUpperCase()}
                </span>
              )}
            </div>
            <div className="flex items-center space-x-3 text-sm text-gray-600">
              <span>{formatPhoneNumber(call.phoneNumber)}</span>
              {call.queueId && (
                <>
                  <span>•</span>
                  <span>From {call.queueId} queue</span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Right Section - Call Timer and Status */}
        <div className="flex items-center space-x-6">
          {/* Call Duration */}
          <div className="flex items-center space-x-2">
            <Clock className="w-4 h-4 text-gray-500" />
            <span className="text-2xl font-mono font-medium text-gray-900">
              {formatTime(duration)}
            </span>
          </div>

          {/* Call Status */}
          <div className="flex items-center">
            <div className={`px-3 py-1 rounded-full text-sm font-medium ${
              call.status === 'active' ? 'bg-green-100 text-green-700' :
              call.status === 'on_hold' ? 'bg-yellow-100 text-yellow-700' :
              call.status === 'connecting' ? 'bg-blue-100 text-blue-700' :
              'bg-gray-100 text-gray-700'
            }`}>
              {call.status === 'active' ? 'Active Call' :
               call.status === 'on_hold' ? 'On Hold' :
               call.status === 'connecting' ? 'Connecting...' :
               'Call Ended'}
            </div>
          </div>
        </div>
      </div>
    </motion.div>
  );
};