import React, { useState, useEffect } from 'react';
import { useSocket } from '../hooks/useSocket';
import { useAuthStore } from '../stores/authStore';
import { SupervisorTopNav } from '../components/supervisor/SupervisorTopNav';
import { NeedsAttentionCards } from '../components/supervisor/NeedsAttentionCards';
import { AgentGrid } from '../components/supervisor/AgentGrid';
import { FocusViewPanel } from '../components/supervisor/FocusViewPanel';
import type { Agent, Call } from '../types/callcenter';

export interface AgentWithCall extends Agent {
  activeCall?: Call;
  callDuration?: number;
  sentiment?: number;
}

export interface Alert {
  id: string;
  type: 'escalating' | 'struggling' | 'high_value' | 'long_wait' | 'negative_sentiment';
  severity: 'critical' | 'warning' | 'info';
  agentId: string;
  agentName: string;
  callId: string;
  title: string;
  description: string;
  actions: string[];
}

export const SupervisorDashboard: React.FC = () => {
  const socket = useSocket();
  const { user } = useAuthStore();

  // State
  const [agents, setAgents] = useState<AgentWithCall[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [focusedAgent, setFocusedAgent] = useState<AgentWithCall | null>(null);
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const [filterStatus, setFilterStatus] = useState<'all' | 'active' | 'available' | 'busy'>('all');
  const [useMockData, setUseMockData] = useState(false); // Toggle for demo data

  // Team metrics
  const [metrics, setMetrics] = useState({
    totalAgents: 0,
    activeAgents: 0,
    totalCalls: 0,
    queueDepth: 0,
    slaCompliance: 0,
    avgHandleTime: 0,
    avgSentiment: 0
  });

  // Load agents and calls on mount
  useEffect(() => {
    if (useMockData) {
      loadMockData();
    } else {
      loadAgents();
      loadMetrics();
    }
  }, [useMockData]);

  // Mock data for demo purposes
  const loadMockData = () => {
    const mockAgents: AgentWithCall[] = [
      {
        id: 'agent-1',
        name: 'Sarah Johnson',
        email: 'sarah@example.com',
        status: 'busy',
        queues: ['sales'],
        activeCall: {
          id: 'call-1',
          phoneNumber: '+12345678900',
          startTime: new Date(Date.now() - 5 * 60 * 1000).toISOString(), // 5 min ago
          status: 'active',
          queueId: 'sales',
          priority: 'high',
          transcription: [
            { speaker: 'caller', text: "I'm very frustrated with this service!", timestamp: new Date(Date.now() - 2 * 60 * 1000).toISOString() },
            { speaker: 'agent', text: "I understand your frustration. Let me help you.", timestamp: new Date(Date.now() - 1.5 * 60 * 1000).toISOString() },
            { speaker: 'caller', text: "This is unacceptable. I want my money back!", timestamp: new Date(Date.now() - 1 * 60 * 1000).toISOString() },
            { speaker: 'agent', text: "I can help you with a refund. Let me pull up your account.", timestamp: new Date(Date.now() - 30 * 1000).toISOString() },
          ],
          sentiment: -0.7
        },
        callDuration: 300,
        sentiment: -0.7
      },
      {
        id: 'agent-2',
        name: 'Mike Chen',
        email: 'mike@example.com',
        status: 'busy',
        queues: ['support'],
        activeCall: {
          id: 'call-2',
          phoneNumber: '+19876543210',
          startTime: new Date(Date.now() - 12 * 60 * 1000).toISOString(), // 12 min ago
          status: 'active',
          queueId: 'support',
          priority: 'medium',
          transcription: [
            { speaker: 'caller', text: "Can you help me reset my password?", timestamp: new Date().toISOString() },
            { speaker: 'agent', text: "Of course! I'll guide you through it.", timestamp: new Date().toISOString() }
          ],
          sentiment: 0.1
        },
        callDuration: 720,
        sentiment: 0.1
      },
      {
        id: 'agent-3',
        name: 'Lisa Martinez',
        email: 'lisa@example.com',
        status: 'available',
        queues: ['billing', 'support'],
      },
      {
        id: 'ai-agent-1',
        name: 'AI-Sales-01',
        email: 'ai-sales-01@system',
        status: 'busy',
        queues: ['sales'],
        activeCall: {
          id: 'call-3',
          phoneNumber: '+15551234567',
          startTime: new Date(Date.now() - 2 * 60 * 1000).toISOString(),
          status: 'ai_active',
          queueId: 'sales',
          priority: 'high',
          transcription: [
            { speaker: 'ai', text: "Hello! How can I help you today?", timestamp: new Date().toISOString() },
            { speaker: 'caller', text: "I'm interested in your enterprise plan.", timestamp: new Date().toISOString() },
            { speaker: 'ai', text: "Great! Let me tell you about our enterprise features...", timestamp: new Date().toISOString() }
          ],
          sentiment: 0.9,
          aiSummary: "High-value lead inquiring about enterprise pricing ($50K+ potential)"
        },
        callDuration: 135,
        sentiment: 0.9
      },
      {
        id: 'ai-agent-2',
        name: 'AI-Support-01',
        email: 'ai-support-01@system',
        status: 'busy',
        queues: ['support'],
        activeCall: {
          id: 'call-4',
          phoneNumber: '+15559876543',
          startTime: new Date(Date.now() - 1 * 60 * 1000).toISOString(),
          status: 'ai_active',
          queueId: 'support',
          priority: 'low',
          transcription: [
            { speaker: 'ai', text: "Hi! I'm here to help. What can I do for you?", timestamp: new Date().toISOString() },
            { speaker: 'caller', text: "Just checking my account balance.", timestamp: new Date().toISOString() }
          ],
          sentiment: 0.8
        },
        callDuration: 60,
        sentiment: 0.8
      },
      {
        id: 'agent-4',
        name: 'David Kim',
        email: 'david@example.com',
        status: 'busy',
        queues: ['support'],
        activeCall: {
          id: 'call-5',
          phoneNumber: '+15551112222',
          startTime: new Date(Date.now() - 8 * 60 * 1000).toISOString(),
          status: 'active',
          queueId: 'support',
          priority: 'medium',
          transcription: [
            { speaker: 'caller', text: "Thank you so much for your help!", timestamp: new Date().toISOString() },
            { speaker: 'agent', text: "You're very welcome! Is there anything else?", timestamp: new Date().toISOString() }
          ],
          sentiment: 0.7
        },
        callDuration: 480,
        sentiment: 0.7
      }
    ];

    const mockAlerts: Alert[] = [
      {
        id: 'alert-1',
        type: 'escalating',
        severity: 'critical',
        agentId: 'agent-1',
        agentName: 'Sarah Johnson',
        callId: 'call-1',
        title: 'Sentiment declining rapidly',
        description: '😟→😡 Sentiment dropped from 0.3 to -0.7 in 2 minutes',
        actions: ['whisper', 'barge']
      },
      {
        id: 'alert-2',
        type: 'struggling',
        severity: 'warning',
        agentId: 'agent-2',
        agentName: 'Mike Chen',
        callId: 'call-2',
        title: 'Long call duration',
        description: 'Call has been active for 12 minutes',
        actions: ['whisper']
      },
      {
        id: 'alert-3',
        type: 'high_value',
        severity: 'info',
        agentId: 'ai-agent-1',
        agentName: 'AI-Sales-01',
        callId: 'call-3',
        title: 'High-value opportunity',
        description: '$50K+ enterprise deal detected by AI',
        actions: ['whisper']
      }
    ];

    setAgents(mockAgents);
    setAlerts(mockAlerts);
    setMetrics({
      totalAgents: 6,
      activeAgents: 5,
      totalCalls: 5,
      queueDepth: 3,
      slaCompliance: 89,
      avgHandleTime: 320,
      avgSentiment: 0.3
    });
  };

  // WebSocket subscriptions for real-time updates
  useEffect(() => {
    if (!socket) return;

    // Agent status changes
    socket.on('agent_status_update', (data: any) => {
      setAgents(prev => prev.map(agent =>
        agent.id === data.agent_id
          ? { ...agent, status: data.status }
          : agent
      ));
    });

    // Call assignments
    socket.on('call_assigned', (data: any) => {
      setAgents(prev => prev.map(agent =>
        agent.id === data.agent_id
          ? { ...agent, activeCall: data.call, callDuration: 0 }
          : agent
      ));
    });

    // Call ended
    socket.on('call_ended', (data: any) => {
      setAgents(prev => prev.map(agent =>
        agent.activeCall?.id === data.call_id
          ? { ...agent, activeCall: undefined, callDuration: undefined }
          : agent
      ));
    });

    // Sentiment updates
    socket.on('sentiment_update', (data: any) => {
      setAgents(prev => prev.map(agent =>
        agent.activeCall?.id === data.call_id
          ? { ...agent, sentiment: data.sentiment }
          : agent
      ));
    });

    // Alert notifications
    socket.on('supervisor_alert', (data: Alert) => {
      setAlerts(prev => [data, ...prev].slice(0, 10)); // Keep last 10
    });


    // Metrics updates
    socket.on('team_metrics', (data: any) => {
      setMetrics(data);
    });

    return () => {
      socket.off('agent_status_update');
      socket.off('call_assigned');
      socket.off('call_ended');
      socket.off('sentiment_update');
      socket.off('supervisor_alert');
      socket.off('team_metrics');
    };
  }, [socket]);

  // Update call durations every second
  useEffect(() => {
    const interval = setInterval(() => {
      setAgents(prev => prev.map(agent =>
        agent.activeCall
          ? { ...agent, callDuration: (agent.callDuration || 0) + 1 }
          : agent
      ));
    }, 1000);

    return () => clearInterval(interval);
  }, []);

  const loadAgents = async () => {
    try {
      const response = await fetch('/api/supervisor/agents', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (response.ok) {
        const data = await response.json();
        setAgents(data.agents || []);
      }
    } catch (error) {
      console.error('Failed to load agents:', error);
    }
  };

  const loadMetrics = async () => {
    try {
      const response = await fetch('/api/supervisor/metrics', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (response.ok) {
        const data = await response.json();
        setMetrics(data);
      }
    } catch (error) {
      console.error('Failed to load metrics:', error);
    }
  };

  const handleMonitor = async (agentId: string) => {
    try {
      await fetch(`/api/supervisor/monitor/${agentId}`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });
      // Set focused agent
      const agent = agents.find(a => a.id === agentId);
      if (agent) setFocusedAgent(agent);
    } catch (error) {
      console.error('Failed to start monitoring:', error);
    }
  };

  const handleWhisper = async (agentId: string) => {
    try {
      await fetch(`/api/supervisor/whisper/${agentId}`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });
    } catch (error) {
      console.error('Failed to start whisper:', error);
    }
  };

  const handleBarge = async (agentId: string) => {
    try {
      await fetch(`/api/supervisor/barge/${agentId}`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });
    } catch (error) {
      console.error('Failed to barge in:', error);
    }
  };

  const handleDismissAlert = (alertId: string) => {
    setAlerts(prev => prev.filter(a => a.id !== alertId));
  };


  // Filter agents based on status
  const filteredAgents = agents.filter(agent => {
    if (filterStatus === 'all') return true;
    if (filterStatus === 'active') return agent.activeCall !== undefined;
    return agent.status === filterStatus;
  });

  return (
    <div className="h-screen flex flex-col bg-gray-50">
      {/* Top Navigation */}
      <SupervisorTopNav
        supervisorName={user?.name || 'Supervisor'}
        metrics={metrics}
      />

      {/* Demo Mode Toggle */}
      <div className="px-6 py-2 bg-yellow-50 border-b border-yellow-200">
        <label className="flex items-center space-x-2 cursor-pointer">
          <input
            type="checkbox"
            checked={useMockData}
            onChange={(e) => setUseMockData(e.target.checked)}
            className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
          />
          <span className="text-sm font-medium text-yellow-900">
            🎭 Demo Mode (Show Mock Data)
          </span>
        </label>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-hidden flex flex-col p-6 space-y-4">
        {/* Needs Attention Cards */}
        {alerts.length > 0 && (
          <NeedsAttentionCards
            alerts={alerts}
            onMonitor={handleMonitor}
            onWhisper={handleWhisper}
            onBarge={handleBarge}
            onDismiss={handleDismissAlert}
          />
        )}

        {/* Agent Monitoring Area */}
        <div className="flex-1 flex space-x-4 overflow-hidden">
          {/* Agent Grid (2/3 width) */}
          <div className="flex-[2] flex flex-col bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
            <AgentGrid
              agents={filteredAgents}
              focusedAgent={focusedAgent}
              viewMode={viewMode}
              filterStatus={filterStatus}
              onViewModeChange={setViewMode}
              onFilterChange={setFilterStatus}
              onAgentSelect={setFocusedAgent}
              onMonitor={handleMonitor}
              onWhisper={handleWhisper}
              onBarge={handleBarge}
            />
          </div>

          {/* Focus View Panel (1/3 width) */}
          <div className="flex-1 bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
            <FocusViewPanel
              agent={focusedAgent}
              onMonitor={handleMonitor}
              onWhisper={handleWhisper}
              onBarge={handleBarge}
            />
          </div>
        </div>
      </div>
    </div>
  );
};
