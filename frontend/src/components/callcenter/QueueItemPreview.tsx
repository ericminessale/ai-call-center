import React from 'react';
import { motion } from 'framer-motion';
import { Clock, Star, RotateCw } from 'lucide-react';
import { cn, formatTime } from '../../lib/utils';
import type { QueuedCall } from '../../types/callcenter';

interface QueueItemPreviewProps {
  call: QueuedCall;
  onSelect: () => void;
}

const priorityColors = {
  low: 'bg-gray-100 text-gray-700',
  medium: 'bg-blue-100 text-blue-700',
  high: 'bg-orange-100 text-orange-700',
  urgent: 'bg-red-100 text-red-700'
};

const sentimentEmojis = {
  very_negative: '😠',
  negative: '😟',
  neutral: '😐',
  positive: '😊',
  very_positive: '😄'
};

export const QueueItemPreview: React.FC<QueueItemPreviewProps> = ({
  call,
  onSelect
}) => {
  return (
    <motion.div
      onClick={onSelect}
      className="p-3 bg-white rounded-lg border hover:border-blue-400 hover:shadow-md transition-all cursor-pointer"
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.99 }}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1">
          {/* Customer Name and Badges */}
          <div className="flex items-center space-x-2 mb-1">
            {/* Priority Badge */}
            <span className={cn(
              'px-2 py-0.5 rounded-full text-xs font-medium',
              priorityColors[call.priority]
            )}>
              {call.priority.toUpperCase()}
            </span>

            {/* Customer Name */}
            <span className="font-medium text-sm text-gray-900">
              {call.customerName || 'Unknown Caller'}
            </span>

            {/* VIP Badge */}
            {call.isVip && (
              <span className="flex items-center px-2 py-0.5 bg-purple-100 text-purple-700 rounded-full text-xs">
                <Star className="w-3 h-3 mr-0.5" />
                VIP
              </span>
            )}

            {/* Returning Customer Badge */}
            {call.returnCustomer && (
              <span className="flex items-center px-2 py-0.5 bg-blue-100 text-blue-700 rounded-full text-xs">
                <RotateCw className="w-3 h-3 mr-0.5" />
                {call.previousCalls}x
              </span>
            )}
          </div>

          {/* AI Summary */}
          {call.aiSummary && (
            <p className="text-xs text-gray-600 line-clamp-2 mt-1">
              {call.aiSummary}
            </p>
          )}

          {/* Phone Number */}
          <p className="text-xs text-gray-500 mt-1">
            {call.phoneNumber}
          </p>
        </div>

        {/* Right Side - Wait Time and Sentiment */}
        <div className="text-right ml-3 flex flex-col items-end">
          {/* Wait Time */}
          <div className="flex items-center text-sm">
            <Clock className={cn(
              'w-3 h-3 mr-1',
              call.waitTime > 300 ? 'text-red-600' :
              call.waitTime > 180 ? 'text-yellow-600' : 'text-gray-600'
            )} />
            <span className={cn(
              'font-medium',
              call.waitTime > 300 ? 'text-red-700' :
              call.waitTime > 180 ? 'text-yellow-700' : 'text-gray-700'
            )}>
              {formatTime(call.waitTime)}
            </span>
          </div>

          {/* Sentiment Indicator */}
          {call.sentiment && (
            <div className="mt-2 text-lg" title={call.sentiment.replace('_', ' ')}>
              {sentimentEmojis[call.sentiment]}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
};