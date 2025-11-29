import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Users,
  Clock,
  TrendingUp,
  TrendingDown,
  Minus,
  AlertTriangle,
  ChevronDown,
  Phone
} from 'lucide-react';
import { cn, formatTime } from '../../lib/utils';
import { QueueItemPreview } from './QueueItemPreview';
import type { Queue } from '../../types/callcenter';

interface QueueCardProps {
  queue: Queue;
  onTakeCall: () => void;
  isAgentAvailable: boolean;
}

export const QueueCard: React.FC<QueueCardProps> = ({
  queue,
  onTakeCall,
  isAgentAvailable
}) => {
  const [isExpanded, setIsExpanded] = useState(false);

  // Determine border and background colors based on severity
  const severityStyles = {
    critical: 'border-red-500 bg-red-50',
    warning: 'border-yellow-500 bg-yellow-50',
    normal: 'border-green-500 bg-green-50'
  };

  // Trend icon component
  const TrendIcon = queue.trend === 'increasing' ? TrendingUp :
                    queue.trend === 'decreasing' ? TrendingDown : Minus;

  const trendColor = queue.trend === 'increasing' ? 'text-red-600' :
                     queue.trend === 'decreasing' ? 'text-green-600' : 'text-gray-600';

  return (
    <div>
      <motion.div
        className={cn(
          'border-l-4 rounded-lg p-4 transition-all duration-200',
          severityStyles[queue.severity],
          queue.severity === 'critical' && 'animate-pulse'
        )}
        whileHover={{ scale: 1.01 }}
      >
        {/* Queue Header */}
        <div className="flex justify-between items-center mb-2">
          <h3 className="font-semibold text-gray-900">{queue.name}</h3>
          <div className="flex items-center space-x-2">
            {/* Waiting Count Badge */}
            <motion.div
              className={cn(
                'flex items-center px-2 py-1 rounded-full text-sm font-medium',
                queue.severity === 'critical' ? 'bg-red-100 text-red-700' :
                queue.severity === 'warning' ? 'bg-yellow-100 text-yellow-700' :
                'bg-green-100 text-green-700'
              )}
              animate={queue.severity === 'critical' ? {
                scale: [1, 1.05, 1],
              } : {}}
              transition={{ repeat: Infinity, duration: 2 }}
            >
              <Users className="w-3 h-3 mr-1" />
              {queue.waiting}
            </motion.div>

            {/* Expand/Collapse Button */}
            {queue.waiting > 0 && (
              <button
                onClick={() => setIsExpanded(!isExpanded)}
                className="p-1 hover:bg-white/50 rounded transition-colors"
              >
                <ChevronDown
                  className={cn(
                    'w-4 h-4 transition-transform',
                    isExpanded && 'rotate-180'
                  )}
                />
              </button>
            )}
          </div>
        </div>

        {/* Metrics Grid */}
        <div className="grid grid-cols-2 gap-2 text-sm mb-3">
          {/* Average Wait Time */}
          <div className="flex items-center">
            <Clock className={cn(
              'w-3 h-3 mr-1',
              queue.avgWait > 180 ? 'text-red-600' : 'text-gray-600'
            )} />
            <span className="text-gray-600">Avg:</span>
            <span className={cn(
              'ml-1 font-medium',
              queue.avgWait > 180 ? 'text-red-700' : 'text-gray-900'
            )}>
              {formatTime(queue.avgWait)}
            </span>
          </div>

          {/* Longest Wait Time */}
          <div className="flex items-center">
            <AlertTriangle className={cn(
              'w-3 h-3 mr-1',
              queue.longest > 300 ? 'text-red-600' : 'text-gray-600'
            )} />
            <span className="text-gray-600">Max:</span>
            <span className={cn(
              'ml-1 font-medium',
              queue.longest > 300 ? 'text-red-700' : 'text-gray-900'
            )}>
              {formatTime(queue.longest)}
            </span>
          </div>
        </div>

        {/* SLA Compliance Bar */}
        <div className="mb-3">
          <div className="flex justify-between items-center mb-1">
            <span className="text-xs text-gray-600">SLA Compliance</span>
            <div className="flex items-center">
              <span className={cn(
                'text-xs font-medium mr-1',
                queue.slaCompliance >= 80 ? 'text-green-700' :
                queue.slaCompliance >= 60 ? 'text-yellow-700' : 'text-red-700'
              )}>
                {queue.slaCompliance}%
              </span>
              <TrendIcon className={cn('w-3 h-3', trendColor)} />
            </div>
          </div>
          <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
            <motion.div
              className={cn(
                'h-full rounded-full',
                queue.slaCompliance >= 80 ? 'bg-green-500' :
                queue.slaCompliance >= 60 ? 'bg-yellow-500' : 'bg-red-500'
              )}
              initial={{ width: 0 }}
              animate={{ width: `${queue.slaCompliance}%` }}
              transition={{ duration: 0.5, ease: 'easeOut' }}
            />
          </div>
        </div>

        {/* Take Call Button */}
        {queue.waiting > 0 && isAgentAvailable && (
          <motion.button
            onClick={onTakeCall}
            className={cn(
              'w-full py-2 px-3 rounded-md font-medium text-sm transition-colors',
              'flex items-center justify-center space-x-2',
              queue.severity === 'critical'
                ? 'bg-red-600 hover:bg-red-700 text-white'
                : queue.severity === 'warning'
                ? 'bg-yellow-600 hover:bg-yellow-700 text-white'
                : 'bg-blue-600 hover:bg-blue-700 text-white'
            )}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Phone className="w-4 h-4" />
            <span>Take Next Call</span>
          </motion.button>
        )}

        {/* Warning Messages */}
        {queue.severity === 'critical' && (
          <div className="mt-2 text-xs text-red-700 font-medium">
            ⚠️ Critical queue depth - immediate attention needed
          </div>
        )}
        {queue.severity === 'warning' && queue.waiting > 5 && (
          <div className="mt-2 text-xs text-yellow-700">
            High queue depth - consider going available
          </div>
        )}
      </motion.div>

      {/* Expanded Queue Details */}
      <AnimatePresence>
        {isExpanded && queue.waiting > 0 && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2 }}
            className="mt-2 ml-4 space-y-2 overflow-hidden"
          >
            {queue.waitingCalls.slice(0, 3).map((call) => (
              <QueueItemPreview
                key={call.id}
                call={call}
                onSelect={() => console.log('Select specific call:', call.id)}
              />
            ))}
            {queue.waitingCalls.length > 3 && (
              <button className="text-sm text-blue-600 hover:underline pl-3">
                View {queue.waitingCalls.length - 3} more...
              </button>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
};