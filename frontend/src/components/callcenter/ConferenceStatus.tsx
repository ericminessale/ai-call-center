import React from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Users, Radio, Headphones, AlertCircle } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { Conference, ConferenceParticipant } from '../../types/callcenter';

interface ConferenceStatusProps {
  isInConference: boolean;
  conference: Conference | null;
  participants: ConferenceParticipant[];
}

export const ConferenceStatus: React.FC<ConferenceStatusProps> = ({
  isInConference,
  conference,
  participants
}) => {

  // Count active participants by type
  const activeParticipants = participants.filter(p => p.status === 'active');
  const customerCount = activeParticipants.filter(p => p.participantType === 'customer').length;
  const agentCount = activeParticipants.filter(p => p.participantType === 'agent').length;

  // Determine status display
  const getStatusInfo = () => {
    if (!isInConference) {
      return {
        label: 'Not Connected',
        color: 'gray',
        icon: AlertCircle,
        description: 'Go available to join your conference'
      };
    }

    if (customerCount === 0) {
      return {
        label: 'Hot Seat Ready',
        color: 'green',
        icon: Headphones,
        description: 'Waiting for customers to be routed'
      };
    }

    return {
      label: 'In Conference',
      color: 'blue',
      icon: Radio,
      description: `${customerCount} customer${customerCount !== 1 ? 's' : ''} connected`
    };
  };

  const statusInfo = getStatusInfo();
  const StatusIcon = statusInfo.icon;

  const colorClasses = {
    gray: {
      bg: 'bg-gray-50',
      border: 'border-gray-200',
      text: 'text-gray-600',
      icon: 'text-gray-400',
      dot: 'bg-gray-400'
    },
    green: {
      bg: 'bg-green-50',
      border: 'border-green-200',
      text: 'text-green-700',
      icon: 'text-green-500',
      dot: 'bg-green-500'
    },
    blue: {
      bg: 'bg-blue-50',
      border: 'border-blue-200',
      text: 'text-blue-700',
      icon: 'text-blue-500',
      dot: 'bg-blue-500'
    }
  };

  const colors = colorClasses[statusInfo.color as keyof typeof colorClasses];

  return (
    <motion.div
      className={cn(
        'rounded-lg border p-3',
        colors.bg,
        colors.border
      )}
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <StatusIcon className={cn('w-4 h-4', colors.icon)} />
          <span className={cn('text-sm font-medium', colors.text)}>
            {statusInfo.label}
          </span>
          {isInConference && (
            <motion.span
              className={cn('w-2 h-2 rounded-full', colors.dot)}
              animate={{ opacity: [1, 0.5, 1] }}
              transition={{ duration: 2, repeat: Infinity }}
            />
          )}
        </div>

        {isInConference && (
          <div className="flex items-center space-x-1 text-xs text-gray-500">
            <Users className="w-3 h-3" />
            <span>{activeParticipants.length}</span>
          </div>
        )}
      </div>

      <p className={cn('text-xs mt-1', colors.text, 'opacity-75')}>
        {statusInfo.description}
      </p>

      {/* Conference name (debug/development info) */}
      {conference && isInConference && (
        <div className="mt-2 pt-2 border-t border-gray-200">
          <p className="text-xs text-gray-400 font-mono truncate">
            {conference.conferenceName}
          </p>
        </div>
      )}

      {/* Participant breakdown when there are multiple */}
      <AnimatePresence>
        {activeParticipants.length > 1 && (
          <motion.div
            className="mt-2 pt-2 border-t border-gray-200"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
          >
            <div className="flex items-center space-x-4 text-xs text-gray-500">
              {customerCount > 0 && (
                <span>{customerCount} customer{customerCount !== 1 ? 's' : ''}</span>
              )}
              {agentCount > 1 && (
                <span>{agentCount} agents</span>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
};
