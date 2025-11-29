import React from 'react';
import { motion } from 'framer-motion';
import {
  Phone,
  Clock,
  Target,
  Star,
  Trophy,
  TrendingUp,
  TrendingDown,
  Award
} from 'lucide-react';
import { cn, formatDuration } from '../../lib/utils';
import type { PerformanceMetrics } from '../../types/callcenter';

interface PerformanceBarProps {
  metrics: PerformanceMetrics;
  position?: 'bottom' | 'static';
}

const MetricPill: React.FC<{
  label: string;
  value: string | number;
  icon?: React.ReactNode;
  target?: number;
  comparison?: { value: number; format?: 'time' | 'number' };
  status?: 'success' | 'warning' | 'danger' | null;
  progress?: boolean;
  suffix?: string;
}> = ({ label, value, icon, target, comparison, status, progress, suffix }) => {
  const statusColors = {
    success: 'text-green-600 bg-green-50',
    warning: 'text-yellow-600 bg-yellow-50',
    danger: 'text-red-600 bg-red-50'
  };

  const getTrend = () => {
    if (!comparison) return null;
    const current = typeof value === 'string' ? parseFloat(value) : value;
    const previous = comparison.value;
    if (current > previous) return 'up';
    if (current < previous) return 'down';
    return 'same';
  };

  const trend = getTrend();

  return (
    <div className="flex items-center space-x-2 px-3 py-2 bg-white rounded-lg border border-gray-200">
      {icon && <div className="text-gray-600">{icon}</div>}
      <div>
        <div className="text-xs text-gray-500">{label}</div>
        <div className="flex items-center space-x-1">
          <span className={cn(
            'text-sm font-semibold',
            status ? statusColors[status] : 'text-gray-900'
          )}>
            {value}{suffix}
          </span>
          {target && progress && (
            <span className="text-xs text-gray-500">/ {target}</span>
          )}
          {trend && (
            <span className="ml-1">
              {trend === 'up' && <TrendingUp className="w-3 h-3 text-green-500" />}
              {trend === 'down' && <TrendingDown className="w-3 h-3 text-red-500" />}
            </span>
          )}
        </div>
      </div>
      {progress && target && (
        <div className="flex-1 max-w-[60px]">
          <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
            <motion.div
              className="h-full bg-blue-500 rounded-full"
              initial={{ width: 0 }}
              animate={{ width: `${Math.min(100, (Number(value) / target) * 100)}%` }}
              transition={{ duration: 0.5 }}
            />
          </div>
        </div>
      )}
    </div>
  );
};

const StreakIndicator: React.FC<{
  label: string;
  days: number;
  milestone: number;
}> = ({ label, days, milestone }) => {
  const progress = (days / milestone) * 100;
  const isClose = days >= milestone - 1;

  return (
    <div className="flex items-center space-x-2 px-3 py-2 bg-white rounded-lg border border-gray-200">
      <Award className={cn(
        'w-5 h-5',
        isClose ? 'text-yellow-500' : 'text-gray-600'
      )} />
      <div>
        <div className="text-xs text-gray-500">{label}</div>
        <div className="flex items-center space-x-1">
          <span className="text-sm font-semibold text-gray-900">{days}</span>
          <span className="text-xs text-gray-500">days</span>
        </div>
      </div>
      {isClose && (
        <motion.div
          className="text-xs text-yellow-600 font-medium"
          animate={{ opacity: [1, 0.5, 1] }}
          transition={{ repeat: Infinity, duration: 2 }}
        >
          {milestone - days === 0 ? 'Milestone!' : `${milestone - days} to go!`}
        </motion.div>
      )}
    </div>
  );
};

export const PerformanceBar: React.FC<PerformanceBarProps> = ({
  metrics,
  position = 'bottom'
}) => {
  return (
    <motion.div
      className={cn(
        'bg-gradient-to-r from-gray-50 to-gray-100 border-t px-6 py-3',
        position === 'bottom' && 'fixed bottom-0 left-0 right-0 z-40'
      )}
      initial={{ y: 50, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ delay: 0.5 }}
    >
      <div className="flex items-center justify-between max-w-7xl mx-auto">
        {/* Calls Today */}
        <MetricPill
          label="Calls Today"
          value={metrics.callsToday}
          icon={<Phone className="w-4 h-4" />}
          target={50}
          progress
        />

        {/* Average Handle Time */}
        <MetricPill
          label="Avg Handle Time"
          value={formatDuration(metrics.avgHandleTime)}
          icon={<Clock className="w-4 h-4" />}
          comparison={{
            value: metrics.avgHandleTimeYesterday,
            format: 'time'
          }}
        />

        {/* First Call Resolution */}
        <MetricPill
          label="First Call Resolution"
          value={metrics.fcr}
          suffix="%"
          icon={<Target className="w-4 h-4" />}
          status={
            metrics.fcr >= 80 ? 'success' :
            metrics.fcr >= 70 ? 'warning' : 'danger'
          }
        />

        {/* CSAT Score */}
        <MetricPill
          label="CSAT Score"
          value={metrics.csat.toFixed(1)}
          icon={<Star className="w-4 h-4 text-yellow-500" />}
          suffix="/5.0"
          status={
            metrics.csat >= 4.5 ? 'success' :
            metrics.csat >= 4.0 ? 'warning' : 'danger'
          }
        />

        {/* Perfect Days Streak */}
        <StreakIndicator
          label="Perfect Days"
          days={metrics.perfectDays}
          milestone={metrics.nextMilestone}
        />

        {/* Personal Best Indicator */}
        {metrics.isPersonalBest && (
          <motion.div
            className="ml-4"
            animate={{
              rotate: [0, -10, 10, -10, 10, 0],
              scale: [1, 1.1, 1]
            }}
            transition={{ duration: 0.5 }}
          >
            <div className="flex items-center space-x-2 px-3 py-2 bg-yellow-100 rounded-lg border border-yellow-300">
              <Trophy className="w-5 h-5 text-yellow-600" />
              <span className="text-sm font-semibold text-yellow-700">Personal Best!</span>
            </div>
          </motion.div>
        )}
      </div>
    </motion.div>
  );
};