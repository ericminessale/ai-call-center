import React from 'react';
import { motion } from 'framer-motion';
import { TrendingUp, Award, Target, Zap } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { PerformanceMetrics } from '../../types/callcenter';

interface PersonalMetricsProps {
  metrics: PerformanceMetrics;
}

export const PersonalMetrics: React.FC<PersonalMetricsProps> = ({ metrics }) => {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wide mb-3">
        Your Performance
      </h3>

      <div className="space-y-3">
        {/* Calls Today Progress */}
        <div>
          <div className="flex justify-between items-center mb-1">
            <span className="text-xs text-gray-600">Daily Goal</span>
            <span className="text-xs font-medium text-gray-900">
              {metrics.callsToday} / 50
            </span>
          </div>
          <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
            <motion.div
              className={cn(
                'h-full rounded-full',
                metrics.callsToday >= 50 ? 'bg-green-500' :
                metrics.callsToday >= 35 ? 'bg-blue-500' :
                metrics.callsToday >= 20 ? 'bg-yellow-500' : 'bg-gray-400'
              )}
              initial={{ width: 0 }}
              animate={{ width: `${Math.min(100, (metrics.callsToday / 50) * 100)}%` }}
              transition={{ duration: 0.5, ease: 'easeOut' }}
            />
          </div>
        </div>

        {/* Key Metrics */}
        <div className="grid grid-cols-2 gap-2">
          {/* FCR */}
          <div className="flex items-center p-2 bg-gray-50 rounded">
            <Target className={cn(
              'w-4 h-4 mr-2',
              metrics.fcr >= 80 ? 'text-green-600' : 'text-gray-600'
            )} />
            <div>
              <div className="text-xs text-gray-500">FCR</div>
              <div className="text-sm font-semibold text-gray-900">{metrics.fcr}%</div>
            </div>
          </div>

          {/* CSAT */}
          <div className="flex items-center p-2 bg-gray-50 rounded">
            <Award className={cn(
              'w-4 h-4 mr-2',
              metrics.csat >= 4.5 ? 'text-yellow-600' : 'text-gray-600'
            )} />
            <div>
              <div className="text-xs text-gray-500">CSAT</div>
              <div className="text-sm font-semibold text-gray-900">{metrics.csat.toFixed(1)}</div>
            </div>
          </div>
        </div>

        {/* Achievement Badges */}
        {metrics.isPersonalBest && (
          <motion.div
            className="flex items-center p-2 bg-gradient-to-r from-yellow-50 to-orange-50 rounded border border-yellow-200"
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', bounce: 0.5 }}
          >
            <Zap className="w-4 h-4 text-yellow-600 mr-2" />
            <span className="text-xs font-medium text-yellow-800">
              Personal Best Today!
            </span>
          </motion.div>
        )}

        {metrics.perfectDays >= 5 && (
          <div className="flex items-center p-2 bg-purple-50 rounded border border-purple-200">
            <Award className="w-4 h-4 text-purple-600 mr-2" />
            <span className="text-xs font-medium text-purple-800">
              {metrics.perfectDays} Day Streak!
            </span>
          </div>
        )}
      </div>
    </div>
  );
};