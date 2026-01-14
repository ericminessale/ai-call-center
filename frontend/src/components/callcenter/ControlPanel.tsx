import React from 'react';
import { motion } from 'framer-motion';
import { QueueCard } from './QueueCard';
import { QuickActions } from './QuickActions';
import { PersonalMetrics } from './PersonalMetrics';
import { ConferenceStatus } from './ConferenceStatus';
import { useCallFabricContext } from '../../contexts/CallFabricContext';
import type { Queue, AgentStatus, PerformanceMetrics } from '../../types/callcenter';

interface ControlPanelProps {
  queues: Queue[];
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  onTakeCall: (queueId: string) => void;
  metrics: PerformanceMetrics;
}

export const ControlPanel: React.FC<ControlPanelProps> = ({
  queues,
  agentStatus,
  onStatusChange,
  onTakeCall,
  metrics
}) => {
  // Conference state for hot seat mode
  const {
    isInConference,
    agentConference,
    conferenceParticipants
  } = useCallFabricContext();

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200">
        <h2 className="font-semibold text-gray-900">Control Center</h2>
      </div>

      {/* Scrollable Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Quick Actions */}
        <QuickActions
          agentStatus={agentStatus}
          onStatusChange={onStatusChange}
          hasWaitingCalls={queues.some(q => q.waiting > 0)}
          onTakeNextCall={() => {
            // Find queue with highest priority call
            const queueWithCalls = queues.find(q => q.waiting > 0);
            if (queueWithCalls) {
              onTakeCall(queueWithCalls.id);
            }
          }}
        />

        {/* Conference Status (Hot Seat Mode) */}
        <ConferenceStatus
          isInConference={isInConference}
          conference={agentConference}
          participants={conferenceParticipants}
        />

        {/* Queue Cards */}
        <div className="space-y-3">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wide">
              Queue Status
            </h3>
            <span className="text-xs text-gray-500">
              Total waiting: {queues.reduce((sum, q) => sum + q.waiting, 0)}
            </span>
          </div>

          {queues.map((queue, index) => (
            <motion.div
              key={queue.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: index * 0.1 }}
            >
              <QueueCard
                queue={queue}
                onTakeCall={() => onTakeCall(queue.id)}
                isAgentAvailable={agentStatus === 'available'}
              />
            </motion.div>
          ))}
        </div>

        {/* Personal Metrics */}
        <PersonalMetrics metrics={metrics} />
      </div>
    </div>
  );
};