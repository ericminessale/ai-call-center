import React from 'react';
import { Brain, TrendingUp, AlertCircle, Award, X, ChevronRight } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { CoachingSuggestion } from '../../pages/SupervisorDashboard';

interface CoachingIntelligenceBarProps {
  suggestions: CoachingSuggestion[];
  onDismiss: (suggestionId: string) => void;
}

export const CoachingIntelligenceBar: React.FC<CoachingIntelligenceBarProps> = ({
  suggestions,
  onDismiss
}) => {
  if (suggestions.length === 0) return null;

  return (
    <div className="bg-gradient-to-r from-purple-50 to-blue-50 border border-purple-200 rounded-lg p-4">
      <div className="flex items-center space-x-2 mb-3">
        <Brain className="w-5 h-5 text-purple-600" />
        <h3 className="text-sm font-semibold text-purple-900">
          💡 AI Coaching Intelligence
        </h3>
      </div>

      <div className="space-y-2">
        {suggestions.map(suggestion => (
          <CoachingCard
            key={suggestion.id}
            suggestion={suggestion}
            onDismiss={onDismiss}
          />
        ))}
      </div>
    </div>
  );
};

const CoachingCard: React.FC<{
  suggestion: CoachingSuggestion;
  onDismiss: (id: string) => void;
}> = ({ suggestion, onDismiss }) => {
  const getIcon = () => {
    switch (suggestion.type) {
      case 'help_needed':
        return <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />;
      case 'pattern':
        return <TrendingUp className="w-5 h-5 text-blue-600 flex-shrink-0" />;
      case 'recognition':
        return <Award className="w-5 h-5 text-green-600 flex-shrink-0" />;
      case 'training':
        return <Brain className="w-5 h-5 text-purple-600 flex-shrink-0" />;
      default:
        return <Brain className="w-5 h-5 text-gray-600 flex-shrink-0" />;
    }
  };

  const getBackgroundColor = () => {
    switch (suggestion.type) {
      case 'help_needed':
        return 'bg-red-50 border-red-200';
      case 'pattern':
        return 'bg-blue-50 border-blue-200';
      case 'recognition':
        return 'bg-green-50 border-green-200';
      case 'training':
        return 'bg-purple-50 border-purple-200';
      default:
        return 'bg-white border-gray-200';
    }
  };

  const getTextColor = () => {
    switch (suggestion.type) {
      case 'help_needed':
        return 'text-red-900';
      case 'pattern':
        return 'text-blue-900';
      case 'recognition':
        return 'text-green-900';
      case 'training':
        return 'text-purple-900';
      default:
        return 'text-gray-900';
    }
  };

  const getLabel = () => {
    switch (suggestion.type) {
      case 'help_needed':
        return '⚠️ Needs Help';
      case 'pattern':
        return '📊 Pattern Detected';
      case 'recognition':
        return '🎯 Recognition';
      case 'training':
        return '📚 Training Opportunity';
      default:
        return '💡 Suggestion';
    }
  };

  return (
    <div className={cn(
      "relative flex items-start space-x-3 p-3 rounded-lg border transition-all hover:shadow-sm",
      getBackgroundColor()
    )}>
      {/* Icon */}
      {getIcon()}

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            {/* Label and Agent Name */}
            <div className="flex items-center space-x-2 mb-1">
              <span className="text-xs font-bold">{getLabel()}</span>
              {suggestion.agentName && (
                <span className={cn("text-xs font-semibold", getTextColor())}>
                  {suggestion.agentName}
                </span>
              )}
            </div>

            {/* Message */}
            <p className={cn("text-sm", getTextColor())}>
              {suggestion.message}
            </p>
          </div>

          {/* Dismiss Button */}
          <button
            onClick={() => onDismiss(suggestion.id)}
            className="ml-2 p-1 hover:bg-white/50 rounded transition-colors flex-shrink-0"
          >
            <X className="w-4 h-4 text-gray-500" />
          </button>
        </div>

        {/* Action Button */}
        {suggestion.action && (
          <button
            onClick={suggestion.action.onClick}
            className={cn(
              "mt-2 inline-flex items-center space-x-1 px-3 py-1.5 rounded text-xs font-medium transition-colors",
              suggestion.type === 'help_needed'
                ? "bg-red-600 text-white hover:bg-red-700"
                : "bg-white border border-gray-300 text-gray-700 hover:bg-gray-50"
            )}
          >
            <span>{suggestion.action.label}</span>
            <ChevronRight className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
};
