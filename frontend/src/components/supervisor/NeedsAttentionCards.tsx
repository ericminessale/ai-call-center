import React from 'react';
import { AlertCircle, TrendingDown, DollarSign, Clock, X, Eye, MessageSquare, PhoneCall } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { Alert } from '../../pages/SupervisorDashboard';

interface NeedsAttentionCardsProps {
  alerts: Alert[];
  onMonitor: (agentId: string) => void;
  onWhisper: (agentId: string) => void;
  onBarge: (agentId: string) => void;
  onDismiss: (alertId: string) => void;
}

export const NeedsAttentionCards: React.FC<NeedsAttentionCardsProps> = ({
  alerts,
  onMonitor,
  onWhisper,
  onBarge,
  onDismiss
}) => {
  // Show max 3 alerts
  const displayedAlerts = alerts.slice(0, 3);

  if (displayedAlerts.length === 0) return null;

  return (
    <div>
      <div className="flex items-center space-x-2 mb-2">
        <AlertCircle className="w-5 h-5 text-orange-600" />
        <h2 className="text-sm font-semibold text-gray-900">
          Needs Attention ({alerts.length} {alerts.length === 1 ? 'alert' : 'alerts'})
        </h2>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {displayedAlerts.map(alert => (
          <AlertCard
            key={alert.id}
            alert={alert}
            onMonitor={onMonitor}
            onWhisper={onWhisper}
            onBarge={onBarge}
            onDismiss={onDismiss}
          />
        ))}
      </div>
    </div>
  );
};

const AlertCard: React.FC<{
  alert: Alert;
  onMonitor: (agentId: string) => void;
  onWhisper: (agentId: string) => void;
  onBarge: (agentId: string) => void;
  onDismiss: (alertId: string) => void;
}> = ({ alert, onMonitor, onWhisper, onBarge, onDismiss }) => {
  const getAlertStyle = () => {
    switch (alert.severity) {
      case 'critical':
        return 'border-red-300 bg-red-50';
      case 'warning':
        return 'border-yellow-300 bg-yellow-50';
      case 'info':
        return 'border-blue-300 bg-blue-50';
      default:
        return 'border-gray-300 bg-gray-50';
    }
  };

  const getAlertIcon = () => {
    switch (alert.type) {
      case 'escalating':
        return <TrendingDown className="w-5 h-5 text-red-600" />;
      case 'struggling':
        return <AlertCircle className="w-5 h-5 text-orange-600" />;
      case 'high_value':
        return <DollarSign className="w-5 h-5 text-green-600" />;
      case 'long_wait':
        return <Clock className="w-5 h-5 text-yellow-600" />;
      case 'negative_sentiment':
        return <TrendingDown className="w-5 h-5 text-red-600" />;
      default:
        return <AlertCircle className="w-5 h-5 text-gray-600" />;
    }
  };

  const getAlertLabel = () => {
    switch (alert.type) {
      case 'escalating':
        return '🔴 ESCALATING';
      case 'struggling':
        return '⚠️ STRUGGLING';
      case 'high_value':
        return '📈 HIGH VALUE';
      case 'long_wait':
        return '⏰ LONG WAIT';
      case 'negative_sentiment':
        return '😟 NEGATIVE';
      default:
        return '⚠️ ATTENTION';
    }
  };

  return (
    <div className={cn("relative border-2 rounded-lg p-4", getAlertStyle())}>
      {/* Dismiss button */}
      <button
        onClick={() => onDismiss(alert.id)}
        className="absolute top-2 right-2 p-1 hover:bg-white/50 rounded transition-colors"
      >
        <X className="w-4 h-4 text-gray-500" />
      </button>

      {/* Header */}
      <div className="flex items-start space-x-2 mb-3">
        {getAlertIcon()}
        <div className="flex-1">
          <div className="text-xs font-bold text-gray-900 mb-1">
            {getAlertLabel()}
          </div>
          <div className="text-sm font-semibold text-gray-900">
            {alert.agentName}
          </div>
          <div className="text-xs text-gray-600 mt-1">
            {alert.title}
          </div>
        </div>
      </div>

      {/* Description */}
      <p className="text-sm text-gray-700 mb-3">
        {alert.description}
      </p>

      {/* Actions */}
      <div className="flex items-center space-x-2">
        <ActionButton
          icon={Eye}
          label="Monitor"
          onClick={() => onMonitor(alert.agentId)}
          variant="primary"
        />
        {alert.actions.includes('whisper') && (
          <ActionButton
            icon={MessageSquare}
            label="Whisper"
            onClick={() => onWhisper(alert.agentId)}
            variant="secondary"
          />
        )}
        {alert.actions.includes('barge') && (
          <ActionButton
            icon={PhoneCall}
            label="Barge"
            onClick={() => onBarge(alert.agentId)}
            variant="secondary"
          />
        )}
      </div>
    </div>
  );
};

const ActionButton: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
  variant: 'primary' | 'secondary';
}> = ({ icon: Icon, label, onClick, variant }) => {
  const baseClasses = "flex items-center space-x-1 px-3 py-1.5 rounded text-xs font-medium transition-colors";
  const variantClasses = variant === 'primary'
    ? "bg-blue-600 text-white hover:bg-blue-700"
    : "bg-white border border-gray-300 text-gray-700 hover:bg-gray-50";

  return (
    <button
      onClick={onClick}
      className={cn(baseClasses, variantClasses)}
    >
      <Icon className="w-3.5 h-3.5" />
      <span>{label}</span>
    </button>
  );
};
