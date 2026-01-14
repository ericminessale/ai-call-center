import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { useSocket } from '../hooks/useSocket';
import { useAuthStore } from '../stores/authStore';
import { useCallFabricContext } from '../contexts/CallFabricContext';
import { GlobalNav } from '../components/callcenter/GlobalNav';
import { ControlPanel } from '../components/callcenter/ControlPanel';
import { Workspace } from '../components/callcenter/Workspace';
import { IntelligencePanel } from '../components/callcenter/IntelligencePanel';
import { PerformanceBar } from '../components/callcenter/PerformanceBar';
import { TransferPanel } from '../components/callcenter/TransferPanel';
import { BrowserPhone } from '../components/callcenter/BrowserPhone';
import { AIInterventionPanel } from '../components/supervisor/AIInterventionPanel';
import type { Call, Queue, AgentStatus, CustomerContext } from '../types/callcenter';

export const CallCenterDashboard: React.FC = () => {
  // Agent State - use context's status which persists across refreshes
  const {
    agentStatus: contextAgentStatus,
    setAgentStatus: setContextAgentStatus,
    joinAgentConference,
    leaveAgentConference,
    isInConference,
    agentConference
  } = useCallFabricContext();

  // Local wrapper state that syncs with context
  const [agentStatus, setAgentStatusLocal] = useState<AgentStatus>(contextAgentStatus);
  const [statusStartTime, setStatusStartTime] = useState<Date>(new Date());

  // Sync local state when context changes (e.g., on restore from localStorage)
  useEffect(() => {
    setAgentStatusLocal(contextAgentStatus);
  }, [contextAgentStatus]);

  // Wrapper that updates both local and context
  const setAgentStatus = (newStatus: AgentStatus) => {
    setAgentStatusLocal(newStatus);
    // Context handles persistence and Call Fabric
  };

  // Call State
  const [activeCall, setActiveCall] = useState<Call | null>(null);
  const [isTransferPanelOpen, setIsTransferPanelOpen] = useState(false);
  const [intelligenceTab, setIntelligenceTab] = useState<'context' | 'ai-intervention'>('context');

  // Queue State
  const [queues, setQueues] = useState<Queue[]>([
    {
      id: 'sales',
      name: 'Sales',
      waiting: 0,
      avgWait: 0,
      longest: 0,
      severity: 'normal',
      trend: 'stable',
      slaCompliance: 100,
      waitingCalls: []
    },
    {
      id: 'support',
      name: 'Support',
      waiting: 0,
      avgWait: 0,
      longest: 0,
      severity: 'normal',
      trend: 'stable',
      slaCompliance: 100,
      waitingCalls: []
    },
    {
      id: 'billing',
      name: 'Billing',
      waiting: 0,
      avgWait: 0,
      longest: 0,
      severity: 'normal',
      trend: 'stable',
      slaCompliance: 100,
      waitingCalls: []
    }
  ]);

  // Customer Context State
  const [customerContext, setCustomerContext] = useState<CustomerContext | null>(null);

  // Performance Metrics
  const [metrics, setMetrics] = useState({
    callsToday: 0,
    avgHandleTime: 0,
    avgHandleTimeYesterday: 0,
    fcr: 0,
    csat: 0,
    perfectDays: 0,
    nextMilestone: 5,
    isPersonalBest: false
  });

  const socket = useSocket();
  const { user } = useAuthStore();

  // WebSocket event handlers
  useEffect(() => {
    if (!socket) return;

    // Queue updates
    socket.on('queue_update', (data: Queue[]) => {
      setQueues(data);
    });

    // Incoming call
    socket.on('call_assigned', (data: any) => {
      setActiveCall(data.call);
      setCustomerContext(data.context);
      setAgentStatus('busy');
      setStatusStartTime(new Date());

      // Show notification
      new Notification('Incoming Call', {
        body: `From ${data.call.customerName || data.call.phoneNumber}`,
        icon: '/icon.png'
      });
    });

    // Call status updates
    socket.on('call_status', (data: any) => {
      if (data.status === 'ended' && activeCall?.id === data.callId) {
        setActiveCall(null);
        setAgentStatus('after-call');
        setStatusStartTime(new Date());
      }
    });

    // AI handoff data
    socket.on('ai_handoff', (data: any) => {
      if (customerContext) {
        setCustomerContext({
          ...customerContext,
          aiSummary: data.summary,
          aiConfidence: data.confidence,
          sentiment: data.sentiment,
          extractedInfo: data.extractedInfo
        });
      }
    });

    // Transcription updates
    socket.on('transcription', (data: any) => {
      if (activeCall && data.callId === activeCall.id) {
        setActiveCall({
          ...activeCall,
          transcription: [...(activeCall.transcription || []), data]
        });
      }
    });

    // Metrics updates
    socket.on('metrics_update', (data: any) => {
      setMetrics(data);
    });

    return () => {
      socket.off('queue_update');
      socket.off('call_assigned');
      socket.off('call_status');
      socket.off('ai_handoff');
      socket.off('transcription');
      socket.off('metrics_update');
    };
  }, [socket, activeCall, customerContext]);

  // Status change handler - delegates to context which handles:
  // - localStorage persistence
  // - Redis sync via socket
  // - Call Fabric online/offline
  // - Conference join/leave
  const handleStatusChange = async (newStatus: AgentStatus) => {
    setStatusStartTime(new Date());
    // Use context's setAgentStatus which handles everything
    await setContextAgentStatus(newStatus);
  };

  // Take call from queue
  const handleTakeCall = async (queueId: string) => {
    const token = localStorage.getItem('access_token');
    socket?.emit('take_call', { queueId, token });
  };

  // Transfer call
  const handleTransfer = async (destination: string, type: 'warm' | 'cold', notes?: string) => {
    if (!activeCall) return;

    const token = localStorage.getItem('access_token');
    socket?.emit('transfer_call', {
      callId: activeCall.id,
      destination,
      type,
      notes,
      context: customerContext,
      token
    });

    setIsTransferPanelOpen(false);
  };

  // End call
  const handleEndCall = async () => {
    if (!activeCall) return;

    const token = localStorage.getItem('access_token');
    socket?.emit('end_call', { callId: activeCall.id, token });
    setActiveCall(null);
    setCustomerContext(null);
    setAgentStatus('after-call');
    setStatusStartTime(new Date());
  };

  // Hold call
  const handleHoldToggle = async () => {
    if (!activeCall) return;

    const newHoldState = !activeCall.isOnHold;
    socket?.emit('hold_call', { callId: activeCall.id, hold: newHoldState });

    setActiveCall({
      ...activeCall,
      isOnHold: newHoldState
    });
  };

  // Mute call
  const handleMuteToggle = async () => {
    if (!activeCall) return;

    // In a real implementation, this would control the microphone
    socket?.emit('mute_toggle', { callId: activeCall.id });
  };

  // Request notification permission on mount
  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyPress = (e: KeyboardEvent) => {
      // Don't trigger shortcuts when typing in input fields
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) {
        return;
      }

      // Only handle shortcuts when there's an active call
      if (!activeCall) return;

      switch (e.key.toLowerCase()) {
        case 'm':
          // Toggle mute
          if (!e.ctrlKey && !e.altKey) {
            handleMuteToggle();
          }
          break;
        case 'h':
          // Toggle hold
          if (!e.ctrlKey && !e.altKey) {
            handleHoldToggle();
          }
          break;
        case 't':
          // Open transfer panel
          if (!e.ctrlKey && !e.altKey) {
            setIsTransferPanelOpen(true);
          }
          break;
        case 'escape':
          // Close transfer panel if open, otherwise prompt to end call
          if (isTransferPanelOpen) {
            setIsTransferPanelOpen(false);
          } else if (activeCall && e.shiftKey) {
            // Shift+ESC to end call (safer than just ESC)
            handleEndCall();
          }
          break;
      }
    };

    window.addEventListener('keydown', handleKeyPress);
    return () => window.removeEventListener('keydown', handleKeyPress);
  }, [activeCall, isTransferPanelOpen]);

  // Generate mock data for demos
  const handleGenerateMockData = async () => {
    try {
      const token = localStorage.getItem('access_token');
      const response = await fetch('/api/queues/mock/generate', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });

      if (response.ok) {
        const result = await response.json();
        console.log('Mock data generated:', result);

        // Fetch the updated queue data
        const queueResponse = await fetch('/api/queues/all/status', {
          headers: {
            'Authorization': `Bearer ${token}`
          }
        });

        if (queueResponse.ok) {
          const queueData = await queueResponse.json();

          // Transform the data to match our Queue type
          const transformedQueues = queueData.map((q: any) => ({
            id: q.queue_id,
            name: q.name,
            waiting: q.depth || 0,
            avgWait: q.average_wait_seconds || 0,
            longest: q.longest_wait_seconds || 0,
            severity: q.depth > 10 ? 'critical' : q.depth > 5 ? 'warning' : 'normal',
            trend: 'stable',
            slaCompliance: 85,
            waitingCalls: []
          }));

          setQueues(transformedQueues);
          console.log('Queues updated:', transformedQueues);
        }
      } else {
        console.error('Failed to generate mock data');
      }
    } catch (error) {
      console.error('Error generating mock data:', error);
    }
  };

  return (
    <motion.div
      className="h-screen flex flex-col bg-gray-50"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3 }}
    >
      {/* Global Navigation Bar */}
      <GlobalNav
        agentStatus={agentStatus}
        agentName={user?.email || 'Agent'}
        onStatusChange={handleStatusChange}
        statusStartTime={statusStartTime}
        onGenerateMockData={handleGenerateMockData}
      />

      {/* Main Content Area - Three Zone Layout */}
      <div className="flex-1 flex overflow-hidden">
        {/* Zone 1: Phone + Control Panel */}
        <motion.div
          className="w-72 border-r border-gray-200 bg-white overflow-y-auto"
          initial={{ x: -100, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          transition={{ delay: 0.1 }}
        >
          {/* Browser Phone */}
          <div className="p-3 border-b border-gray-200">
            <BrowserPhone
              onCallStart={(call) => {
                setActiveCall({
                  id: call.id,
                  customerName: call.callerId,
                  phoneNumber: call.callerId,
                  startTime: new Date().toISOString(),
                  status: 'active',
                  queueId: '',
                  priority: 'medium',
                  sentiment: 0,
                  aiSummary: call.aiContext?.summary
                });
              }}
              onCallEnd={() => {
                setActiveCall(null);
                setAgentStatus('after-call');
              }}
            />
          </div>

          {/* Control Panel */}
          <ControlPanel
            queues={queues}
            agentStatus={agentStatus}
            onStatusChange={handleStatusChange}
            onTakeCall={handleTakeCall}
            metrics={metrics}
          />
        </motion.div>

        {/* Zone 2: Workspace */}
        <motion.div
          className="flex-1 flex flex-col bg-gray-50"
          initial={{ y: 20, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          transition={{ delay: 0.2 }}
        >
          <Workspace
            activeCall={activeCall}
            onHold={handleHoldToggle}
            onTransfer={() => setIsTransferPanelOpen(true)}
            onEndCall={handleEndCall}
          />
        </motion.div>

        {/* Zone 3: Intelligence & Monitoring */}
        <motion.div
          className="w-96 border-l border-gray-200 bg-white flex flex-col"
          initial={{ x: 100, opacity: 0 }}
          animate={{ x: 0, opacity: 1 }}
          transition={{ delay: 0.3 }}
        >
          {/* Tabs */}
          <div className="border-b border-gray-200">
            <nav className="flex">
              <button
                onClick={() => setIntelligenceTab('context')}
                className={`
                  flex-1 px-4 py-3 text-sm font-medium transition-colors
                  ${intelligenceTab === 'context'
                    ? 'border-b-2 border-blue-500 text-blue-600 bg-blue-50'
                    : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'}
                `}
              >
                Call Context
              </button>
              <button
                onClick={() => setIntelligenceTab('ai-intervention')}
                className={`
                  flex-1 px-4 py-3 text-sm font-medium transition-colors
                  ${intelligenceTab === 'ai-intervention'
                    ? 'border-b-2 border-purple-500 text-purple-600 bg-purple-50'
                    : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'}
                `}
              >
                AI Intervention
              </button>
            </nav>
          </div>

          {/* Tab Content */}
          <div className="flex-1 overflow-y-auto">
            {intelligenceTab === 'context' ? (
              <IntelligencePanel
                customerContext={customerContext}
                activeCall={activeCall}
              />
            ) : (
              <div className="p-4">
                <AIInterventionPanel />
              </div>
            )}
          </div>
        </motion.div>
      </div>

      {/* Performance Bar */}
      <PerformanceBar metrics={metrics} />

      {/* Transfer Panel (Slide-over) */}
      <TransferPanel
        isOpen={isTransferPanelOpen}
        onClose={() => setIsTransferPanelOpen(false)}
        onTransfer={handleTransfer}
        currentCall={activeCall}
        queues={queues}
      />
    </motion.div>
  );
};