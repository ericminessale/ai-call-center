import React from 'react';
import { Users, Phone, Clock, TrendingUp, TrendingDown } from 'lucide-react';
import { cn } from '../../lib/utils';

interface SupervisorTopNavProps {
  supervisorName: string;
  metrics: {
    totalAgents: number;
    activeAgents: number;
    totalCalls: number;
    queueDepth: number;
    slaCompliance: number;
    avgHandleTime: number;
    avgSentiment: number;
  };
}

export const SupervisorTopNav: React.FC<SupervisorTopNavProps> = ({
  supervisorName,
  metrics
}) => {
  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const getSLAColor = (sla: number) => {
    if (sla >= 90) return 'text-green-600';
    if (sla >= 80) return 'text-yellow-600';
    return 'text-red-600';
  };

  const getSLATrend = (sla: number) => {
    if (sla >= 90) return <TrendingUp className="w-4 h-4 text-green-600" />;
    return <TrendingDown className="w-4 h-4 text-red-600" />;
  };

  return (
    <div className="bg-white border-b border-gray-200 px-6 py-4">
      <div className="flex items-center justify-between">
        {/* Left: Title */}
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Supervisor Dashboard</h1>
          <p className="text-sm text-gray-600">{supervisorName}</p>
        </div>

        {/* Center: Team Metrics */}
        <div className="flex items-center space-x-6">
          {/* Agents */}
          <MetricBadge
            icon={Users}
            label="Agents"
            value={`${metrics.activeAgents}/${metrics.totalAgents}`}
            color="blue"
          />

          {/* Calls */}
          <MetricBadge
            icon={Phone}
            label="Calls"
            value={metrics.totalCalls.toString()}
            color="green"
          />

          {/* Queue */}
          <MetricBadge
            icon={Clock}
            label="Queue"
            value={metrics.queueDepth.toString()}
            color={metrics.queueDepth > 10 ? 'red' : 'gray'}
          />

          {/* SLA */}
          <div className="flex items-center space-x-2 px-3 py-2 bg-gray-50 rounded-lg">
            <div>
              <div className="text-xs text-gray-600">SLA</div>
              <div className={cn("text-lg font-semibold flex items-center space-x-1", getSLAColor(metrics.slaCompliance))}>
                <span>{metrics.slaCompliance}%</span>
                {getSLATrend(metrics.slaCompliance)}
              </div>
            </div>
          </div>

          {/* Avg Handle Time */}
          <div className="flex items-center space-x-2 px-3 py-2 bg-gray-50 rounded-lg">
            <div>
              <div className="text-xs text-gray-600">Avg Handle Time</div>
              <div className="text-lg font-semibold text-gray-900">
                {formatTime(metrics.avgHandleTime)}
              </div>
            </div>
          </div>

          {/* Avg Sentiment */}
          <div className="flex items-center space-x-2 px-3 py-2 bg-gray-50 rounded-lg">
            <div>
              <div className="text-xs text-gray-600">Avg Sentiment</div>
              <div className="text-lg font-semibold flex items-center space-x-1">
                {getSentimentDisplay(metrics.avgSentiment)}
              </div>
            </div>
          </div>
        </div>

        {/* Right: Team Filter (placeholder) */}
        <div>
          <select className="px-3 py-2 bg-white border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
            <option>All Teams</option>
            <option>Sales</option>
            <option>Support</option>
            <option>Billing</option>
          </select>
        </div>
      </div>
    </div>
  );
};

const MetricBadge: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  color: 'blue' | 'green' | 'red' | 'gray';
}> = ({ icon: Icon, label, value, color }) => {
  const colors = {
    blue: 'bg-blue-50 text-blue-700',
    green: 'bg-green-50 text-green-700',
    red: 'bg-red-50 text-red-700',
    gray: 'bg-gray-50 text-gray-700'
  };

  return (
    <div className={cn("flex items-center space-x-2 px-3 py-2 rounded-lg", colors[color])}>
      <Icon className="w-5 h-5" />
      <div>
        <div className="text-xs opacity-80">{label}</div>
        <div className="text-lg font-semibold">{value}</div>
      </div>
    </div>
  );
};

const getSentimentDisplay = (sentiment: number) => {
  if (sentiment > 0.5) {
    return (
      <>
        <span className="text-green-600">😊</span>
        <span className="text-green-600">{sentiment.toFixed(2)}</span>
      </>
    );
  }
  if (sentiment < -0.5) {
    return (
      <>
        <span className="text-red-600">😟</span>
        <span className="text-red-600">{sentiment.toFixed(2)}</span>
      </>
    );
  }
  return (
    <>
      <span className="text-gray-600">😐</span>
      <span className="text-gray-600">{sentiment.toFixed(2)}</span>
    </>
  );
};
