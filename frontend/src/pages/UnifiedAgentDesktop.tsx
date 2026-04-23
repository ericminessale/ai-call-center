import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { useCallFabricContext, ConnectedCustomer } from '../contexts/CallFabricContext';
import { useSocket } from '../hooks/useSocket';
import { UnifiedHeader } from '../components/unified/UnifiedHeader';
import { LeftPanel } from '../components/unified/LeftPanel';
import { IncomingCallBanner } from '../components/unified/IncomingCallBanner';
import { SettingsPanel } from '../components/unified/SettingsPanel';
import { ContactDetailView } from '../components/contacts/ContactDetailView';
import { contactsApi, callsApi, queueApi, callControlApi } from '../services/api';
import { Contact, ContactMinimal, Call, QueueConfig } from '../types/callcenter';
import type { SentimentData } from '../components/contacts/LiveCallTab';
import { Users, Phone, ListTodo } from 'lucide-react';
import { ContactDetailSkeleton } from '../components/shared/Skeleton';
import { DashboardCharts } from '../components/unified/DashboardCharts';
import toast from 'react-hot-toast';
import { logger } from '../lib/logger';
import { mapCall, mapCalls } from '../lib/mapCall';

// View modes for the unified interface
export type ViewMode = 'contacts' | 'calls' | 'queue' | 'supervisor' | 'settings';

// Agent status options
export type AgentStatus = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

export function UnifiedAgentDesktop() {
  const { contactId, callId } = useParams<{ contactId?: string; callId?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuthStore();

  // Determine initial view mode from URL
  const getInitialViewMode = (): ViewMode => {
    if (location.pathname.startsWith('/calls')) return 'calls';
    if (location.pathname.startsWith('/queue')) return 'queue';
    if (location.pathname.startsWith('/supervisor')) return 'supervisor';
    if (location.pathname.startsWith('/settings')) return 'settings';
    return 'contacts';
  };

  // View state
  const [viewMode, setViewMode] = useState<ViewMode>(getInitialViewMode());

  // Agent stats
  const [stats, setStats] = useState({
    callsToday: 0,
    avgHandleTime: 0,
    queueDepth: 0,
    longestWait: 0,
  });

  // Contact state
  const [contacts, setContacts] = useState<ContactMinimal[]>([]);
  const [selectedContact, setSelectedContact] = useState<Contact | null>(null);
  const [isLoadingContactDetail, setIsLoadingContactDetail] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoadingContacts, setIsLoadingContacts] = useState(true);

  // Call state
  const [activeCalls, setActiveCalls] = useState<Call[]>([]);
  const [queuedCalls, setQueuedCalls] = useState<Call[]>([]);
  const [isLoadingCalls, setIsLoadingCalls] = useState(true);
  const [isLoadingQueue, setIsLoadingQueue] = useState(true);
  const [callCounts, setCallCounts] = useState({
    active: 0,
    queue: 0,
    aiActive: 0,
  });

  // Queue configs for filter pills + badges
  const [queueConfigs, setQueueConfigs] = useState<QueueConfig[]>([]);

  // Live sentiment from AI agents, keyed by call ID
  const [liveSentimentMap, setLiveSentimentMap] = useState<Record<number, SentimentData>>({});

  // Call Fabric integration (shared context)
  const callFabric = useCallFabricContext();

  // Socket connection (proper authentication and reconnection handling)
  const socket = useSocket();

  // Ref to track if we've loaded initial data
  const initialLoadDone = useRef(false);

  // WebSocket subscriptions
  useEffect(() => {
    if (!socket) return;

    // Remove any existing listeners first to prevent duplicates
    socket.off('call_update');
    socket.off('call_assigned');
    socket.off('call_ended');
    socket.off('contact_update');
    socket.off('agent_stats');
    socket.off('authenticated');
    socket.off('connect');
    socket.off('queue_update');
    socket.off('sentiment_update');

    // Handle socket connect/reconnect - reload data when connected
    socket.on('connect', () => {
      logger.debug('[Unified] Socket connected:', socket.id);
    });

    // Handle authentication success - reload data
    socket.on('authenticated', () => {
      logger.debug('[Unified] Socket authenticated, reloading data');
      loadActiveCalls();
      loadQueuedCalls();
      updateCallCounts();
    });

    // Call updates
    socket.on('call_update', (data: { call: Call }) => {
      logger.debug('[Unified] call_update:', data);
      const call = data.call;
      if (!call) return;

      const mappedCall = mapCall(call);
      const isEnded = ['ended', 'completed'].includes(mappedCall.status);

      setActiveCalls(prev => {
        if (isEnded) return prev.filter(c => c.id !== mappedCall.id);
        const exists = prev.find(c => c.id === mappedCall.id);
        if (exists) return prev.map(c => c.id === mappedCall.id ? mappedCall : c);
        return [...prev, mappedCall];
      });
      // Also remove ended calls from queuedCalls so activeCallForContact clears
      if (isEnded) {
        setQueuedCalls(prev => prev.filter(c => c.id !== mappedCall.id));
      }
      updateCallCounts();
    });

    // New call assigned to this agent
    socket.on('call_assigned', (data: { call: Call }) => {
      logger.debug('[Unified] call_assigned:', data);
      const call = data.call;
      if (call) {
        setActiveCalls(prev => [...prev, call]);
        if (call.contact_id) {
          loadContactDetail(call.contact_id);
        }
        updateCallCounts();
      }
    });

    // Call ended
    socket.on('call_ended', (data: { callId: number }) => {
      logger.debug('[Unified] call_ended:', data);
      setActiveCalls(prev => prev.filter(c => c.id !== data.callId));
      setQueuedCalls(prev => prev.filter(c => c.id !== data.callId));
      updateCallCounts();
    });

    // Contact updated (e.g., when AI agent collects customer name)
    socket.on('contact_update', (data: { contact: any }) => {
      logger.debug('[Unified] contact_update:', data);
      const updatedContact = data.contact;
      if (!updatedContact) return;

      setContacts(prev => prev.map(c =>
        c.id === updatedContact.id ? { ...c, ...updatedContact } : c
      ));

      if (selectedContact?.id === updatedContact.id) {
        setSelectedContact(prev => prev ? { ...prev, ...updatedContact } : null);
      }
    });

    // Agent stats update
    socket.on('agent_stats', (newStats: typeof stats) => {
      logger.debug('[Unified] agent_stats:', newStats);
      setStats(newStats);
    });

    // Queue config changed - admin CRUD on queues
    socket.on('queue_config_changed', () => {
      logger.debug('[Unified] queue_config_changed — reload queue data');
      loadQueueConfigs();
    });

    // Queue update - call added, assigned, or removed from queue
    socket.on('queue_update', (data: { call: Call; queue_id: string; action: string; assigned_agent_id?: number; assigned_agent_name?: string }) => {
      logger.debug('[Unified] queue_update:', data);
      const { call, action } = data;
      if (!call) return;

      const mappedCall = mapCall(call);

      setQueuedCalls(prev => {
        switch (action) {
          case 'added':
            if (!prev.find(c => c.id === mappedCall.id)) return [...prev, mappedCall];
            return prev;
          case 'assigned':
            return prev.map(c => c.id === mappedCall.id ? mappedCall : c);
          case 'removed':
          case 'active':
          case 'ended':
            return prev.filter(c => c.id !== mappedCall.id);
          default: {
            const exists = prev.find(c => c.id === mappedCall.id);
            if (exists) return prev.map(c => c.id === mappedCall.id ? mappedCall : c);
            return [...prev, mappedCall];
          }
        }
      });

      updateCallCounts();
    });

    // Real-time sentiment updates from AI agents
    socket.on('sentiment_update', (data: { callId: number; score: number; reason?: string; timestamp?: string }) => {
      logger.debug('[Unified] sentiment_update:', data);
      setLiveSentimentMap(prev => ({
        ...prev,
        [data.callId]: { score: data.score, reason: data.reason, timestamp: data.timestamp },
      }));

      // Also update the sentiment field on the matching active call
      setActiveCalls(prev => prev.map(c =>
        (c.id === data.callId) ? { ...c, sentiment: data.score } : c
      ));
    });

    return () => {
      socket.off('call_update');
      socket.off('call_assigned');
      socket.off('call_ended');
      socket.off('contact_update');
      socket.off('agent_stats');
      socket.off('authenticated');
      socket.off('connect');
      socket.off('queue_update');
      socket.off('queue_config_changed');
      socket.off('sentiment_update');
    };
  }, [socket]);

  // Load contacts
  const loadContacts = useCallback(async () => {
    try {
      const response = await contactsApi.list({
        search: searchQuery || undefined,
        per_page: 100,
        sort_by: 'last_interaction',
      });
      setContacts(response.data.contacts);
    } catch (error) {
      logger.error('Failed to load contacts:', error);
    } finally {
      setIsLoadingContacts(false);
    }
  }, [searchQuery]);

  // Load contact detail
  const loadContactDetail = useCallback(async (id: number) => {
    setIsLoadingContactDetail(true);
    try {
      const response = await contactsApi.get(id);
      setSelectedContact(response.data);
    } catch (error) {
      logger.error('Failed to load contact:', error);
      setSelectedContact(null);
    } finally {
      setIsLoadingContactDetail(false);
    }
  }, []);

  // Load active calls
  const loadActiveCalls = useCallback(async () => {
    try {
      const response = await callsApi.list({ status: 'active,ai_active,connecting,ringing' });
      setActiveCalls(mapCalls(response.data.calls));
    } catch (error) {
      logger.error('Failed to load active calls:', error);
    } finally {
      setIsLoadingCalls(false);
    }
  }, []);

  // Load queued calls - includes 'waiting', 'assigned', and computed 'urgent' status
  const loadQueuedCalls = useCallback(async () => {
    try {
      const response = await callsApi.getQueuedCalls();
      setQueuedCalls(mapCalls(response.data.calls));
    } catch (error) {
      logger.error('Failed to load queued calls:', error);
      // Fallback to old endpoint if new one doesn't exist
      try {
        const response = await callsApi.list({ status: 'waiting,assigned' });
        setQueuedCalls(response.data.calls || []);
      } catch (fallbackError) {
        logger.error('Fallback also failed:', fallbackError);
      }
    } finally {
      setIsLoadingQueue(false);
    }
  }, []);

  // Update call counts
  const updateCallCounts = useCallback(async () => {
    try {
      const [activeRes, queueRes] = await Promise.all([
        callsApi.list({ status: 'active,ai_active', per_page: 1 }),
        callsApi.list({ status: 'waiting,assigned', per_page: 1 }),
      ]);
      setCallCounts({
        active: activeRes.data.total || 0,
        queue: queueRes.data.total || 0,
        aiActive: activeRes.data.calls?.filter((c: Call) => c.status === 'ai_active').length || 0,
      });
    } catch (error) {
      logger.error('Failed to update call counts:', error);
    }
  }, []);

  // Load active queue configs (for filter pills and badges)
  const loadQueueConfigs = useCallback(async () => {
    try {
      const response = await queueApi.getActiveQueueConfigs();
      setQueueConfigs(response.data.queues || []);
    } catch (error) {
      logger.error('Failed to load queue configs:', error);
    }
  }, []);

  // Load agent stats from backend
  const loadStats = useCallback(async () => {
    try {
      const response = await callsApi.getMyStats();
      if (response.data.success) {
        setStats(response.data.stats);
      }
    } catch (error) {
      logger.error('Failed to load agent stats:', error);
    }
  }, []);

  // Initial data load
  useEffect(() => {
    loadContacts();
    loadActiveCalls();
    loadQueuedCalls();
    updateCallCounts();
    loadQueueConfigs();
    loadStats();
  }, []);

  // Poll stats every 30 seconds
  useEffect(() => {
    const interval = setInterval(loadStats, 30000);
    return () => clearInterval(interval);
  }, [loadStats]);

  // Handle customer connected to agent's conference (auto-navigation)
  useEffect(() => {
    const handleCustomerConnected = async (customer: ConnectedCustomer) => {
      logger.debug('[Unified] Customer connected:', customer);

      const customerName = customer.aiContext?.customer_name ||
                          customer.customerInfo.name ||
                          customer.callerNumber;
      toast.success(`Customer connected: ${customerName}`, {
        duration: 5000,
        icon: '📞',
      });

      await loadActiveCalls();

      // Use contact_id from the event if backend already created/updated the contact
      if (customer.customerInfo.contact_id) {
        setViewMode('contacts');
        navigate(`/contacts/${customer.customerInfo.contact_id}`);
        loadContactDetail(customer.customerInfo.contact_id);
        loadContacts();
        return;
      }

      // Fallback: If no contact_id in event, try to find or create the contact
      try {
        const response = await contactsApi.lookupOrCreate({
          phone: customer.callerNumber,
          displayName: customer.aiContext?.customer_name || customer.customerInfo.name,
          company: customer.aiContext?.company,
        });

        const contact = response.data.contact;
        setViewMode('contacts');
        navigate(`/contacts/${contact.id}`);
        loadContactDetail(contact.id);
        loadContacts();
      } catch (error) {
        logger.error('Failed to lookup/create contact:', error);
        setViewMode('calls');
        navigate('/calls');
      }
    };

    callFabric.setOnCustomerConnected(handleCustomerConnected);

    return () => {
      callFabric.setOnCustomerConnected(undefined);
    };
  }, [callFabric, navigate, loadActiveCalls, loadContactDetail, loadContacts]);

  // Reload contacts on search change (debounced)
  useEffect(() => {
    const timer = setTimeout(() => {
      loadContacts();
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery, loadContacts]);

  // Load contact when URL changes
  useEffect(() => {
    if (contactId) {
      const id = parseInt(contactId, 10);
      if (!isNaN(id)) {
        loadContactDetail(id);
      }
    } else if (callId) {
      const call = activeCalls.find(c => c.id === parseInt(callId, 10));
      if (call?.contact_id) {
        loadContactDetail(call.contact_id);
      }
    } else {
      setSelectedContact(null);
    }
  }, [contactId, callId, loadContactDetail, activeCalls]);

  // Update view mode when URL changes
  useEffect(() => {
    setViewMode(getInitialViewMode());
  }, [location.pathname]);

  // Tabs permitted per role. Agents have no supervisor/settings access;
  // supervisors can monitor but not configure the system.
  const canAccessView = useCallback(
    (mode: ViewMode): boolean => {
      if (mode === 'settings') return user?.role === 'admin';
      if (mode === 'supervisor') return user?.role === 'admin' || user?.role === 'supervisor';
      return true;
    },
    [user?.role],
  );

  // If the URL points at a restricted view, bounce back to the default view.
  // Runs whenever the user or URL changes so token refresh / role change updates flow through.
  useEffect(() => {
    if (!user) return;
    if (!canAccessView(viewMode)) {
      navigate('/', { replace: true });
    }
  }, [user, viewMode, canAccessView, navigate]);

  // Handle view mode change
  const handleViewModeChange = (mode: ViewMode) => {
    if (!canAccessView(mode)) {
      toast.error('You do not have access to that view');
      return;
    }
    setViewMode(mode);
    switch (mode) {
      case 'contacts':
        navigate(selectedContact ? `/contacts/${selectedContact.id}` : '/');
        break;
      case 'calls':
        navigate('/calls');
        break;
      case 'queue':
        navigate('/queue');
        break;
      case 'supervisor':
        navigate('/supervisor');
        break;
      case 'settings':
        navigate('/settings');
        break;
    }
  };

  // Handle contact selection
  const handleContactSelect = (contact: ContactMinimal) => {
    navigate(`/contacts/${contact.id}`);
  };

  // Handle contact update
  const handleContactUpdate = (updatedContact: Contact) => {
    setSelectedContact(updatedContact);
    setContacts(prev =>
      prev.map(c =>
        c.id === updatedContact.id
          ? {
              ...c,
              displayName: updatedContact.displayName,
              phone: updatedContact.phone,
              company: updatedContact.company,
              accountTier: updatedContact.accountTier,
              isVip: updatedContact.isVip,
              totalCalls: updatedContact.totalCalls,
              lastInteractionAt: updatedContact.lastInteractionAt,
            }
          : c
      )
    );
  };

  // Handle contact delete
  const handleContactDelete = (contactId: number) => {
    setContacts(prev => prev.filter(c => c.id !== contactId));
    setSelectedContact(null);
    navigate('/');
  };

  // Handle new contact created
  const handleContactCreated = (newContact: ContactMinimal) => {
    setContacts(prev => [newContact, ...prev]);
    navigate(`/contacts/${newContact.id}`);
  };

  // Handle call selection (from Active Calls view)
  const handleCallSelect = async (call: Call) => {
    if (call.contact_id) {
      await loadContactDetail(call.contact_id);
      navigate(`/contacts/${call.contact_id}`);
    } else {
      const phoneNumber = call.from_number || call.phoneNumber || (call as any).fromNumber;

      if (phoneNumber && phoneNumber !== 'Unknown' && phoneNumber !== 'unknown') {
        try {
          const response = await contactsApi.lookupOrCreate({
            phone: phoneNumber,
            displayName: phoneNumber,
          });

          const contactId = response.data.contact?.id || response.data.id;
          if (contactId) {
            loadContacts();
            navigate(`/contacts/${contactId}`);
          }
        } catch (error) {
          logger.error('Failed to lookup/create contact:', error);
        }
      }
    }
    setViewMode('contacts');
  };

  // Handle take call from queue
  const handleTakeCall = async (call: Call) => {
    try {
      const response = await callsApi.take(call.id);
      const conferenceName = response.data.conference_name || call.conference_name;

      if (conferenceName) {
        const callAssignment = {
          callId: call.signalwire_call_sid || String(call.id),
          callDbId: Number(call.id),
          conferenceName: conferenceName,
          queueId: call.queue_id || '',
          callerNumber: call.from_number || call.phoneNumber || '',
          customerInfo: {
            phone: call.from_number || call.phoneNumber || '',
            name: call.contact?.displayName || call.customerName,
            contact_id: call.contact_id,
          },
          context: (call as any).aiContext || {},
        };

        await callFabric.acceptCallAssignmentWithData(callAssignment);
      }

      handleCallSelect(call);
    } catch (error) {
      logger.error('Failed to take call:', error);
      toast.error('Failed to take call');
    }
  };

  // Handle incoming call answer
  const handleAnswerIncoming = async (phoneNumber: string) => {
    try {
      const response = await contactsApi.lookupOrCreate({
        phone: phoneNumber,
        displayName: phoneNumber,
      });
      await callFabric.answerCall();
      navigate(`/contacts/${response.data.id}`);
      setViewMode('contacts');
    } catch (error) {
      logger.error('Failed to handle incoming call:', error);
      await callFabric.answerCall();
    }
  };

  // Handle incoming call decline
  const handleDeclineIncoming = async () => {
    await callFabric.hangup();
  };

  // Handle outbound call started from QuickDial - navigate to contact page
  const handleOutboundCallStarted = async (phoneNumber: string) => {
    logger.debug('[Unified] Outbound call started to:', phoneNumber);
    try {
      const response = await contactsApi.lookupOrCreate({
        phone: phoneNumber,
        displayName: phoneNumber,
      });

      const contactId = response.data.contact?.id || response.data.id;
      if (contactId) {
        loadContacts();
        navigate(`/contacts/${contactId}`);
        setViewMode('contacts');
      }
    } catch (error) {
      logger.error('Failed to lookup/create contact for outbound call:', error);
    }
  };

  // Handle accepting call assignment from banner
  const handleAcceptAssignment = async () => {
    if (callFabric.pendingCallAssignment) {
      try {
        // Auto-stop any active tap/monitor on this call before joining as participant
        const assignedCallId = callFabric.pendingCallAssignment.callDbId;
        if (assignedCallId) {
          try {
            await callControlApi.stopMonitor(assignedCallId);
            console.log('[Unified] Stopped monitoring before accepting assignment');
          } catch {
            // Ignore - may not have been monitoring
          }
        }
        await callFabric.acceptCallAssignment();
        const contactId = callFabric.pendingCallAssignment.customerInfo?.contact_id ||
                         callFabric.pendingCallAssignment.customerInfo?.contactId;
        if (contactId) {
          navigate(`/contacts/${contactId}`);
          setViewMode('contacts');
        }
      } catch (error: any) {
        logger.error('Failed to accept call assignment:', error);
        const detail = error?.response?.data?.error || error?.message || 'Unknown error';
        toast.error(`Failed to accept call: ${detail}`);
      }
    }
  };

  return (
    <div className="h-screen flex flex-col bg-canvas">
      {/* Incoming Call Banner - show for inbound calls */}
      {callFabric.callState === 'ringing' && callFabric.activeCall && callFabric.activeCall.direction === 'inbound' && (
        <IncomingCallBanner
          phoneNumber={callFabric.activeCall.callerId || 'Unknown'}
          onAnswer={() => handleAnswerIncoming(callFabric.activeCall?.callerId || '')}
          onDecline={handleDeclineIncoming}
        />
      )}

      {/* Call Assignment Banner - show when customer routed from queue, backup request, or escalation */}
      {callFabric.pendingCallAssignment && !callFabric.isInConference && (
        <IncomingCallBanner
          phoneNumber={callFabric.pendingCallAssignment.callerNumber || callFabric.pendingCallAssignment.customerInfo?.phone || 'Unknown'}
          callerName={callFabric.pendingCallAssignment.customerInfo?.name}
          queueId={callFabric.pendingCallAssignment.queueId}
          aiContext={callFabric.pendingCallAssignment.context}
          onAnswer={handleAcceptAssignment}
          onDecline={callFabric.rejectCallAssignment}
          assignmentType={callFabric.pendingCallAssignment.assignmentType}
          requestingAgent={callFabric.pendingCallAssignment.requestingAgent}
          whisperMode={callFabric.pendingCallAssignment.whisperMode}
        />
      )}

      {/* Header */}
      <UnifiedHeader
        user={user}
        agentStatus={callFabric.agentStatus as AgentStatus}
        onStatusChange={(status) => callFabric.setAgentStatus(status as any)}
        stats={stats}
        viewMode={viewMode}
        onViewModeChange={handleViewModeChange}
        callCounts={callCounts}
        onLogout={logout}
        callFabric={callFabric}
        onOutboundCallStarted={handleOutboundCallStarted}
      />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {viewMode === 'settings' ? (
          /* Full-width settings panel */
          <div className="flex-1 bg-canvas overflow-hidden">
            <SettingsPanel />
          </div>
        ) : (
          <>
            {/* Left Panel — sits on page surface; depth comes from detail pane being raised */}
            <div className="w-[340px] border-r border-rule flex flex-col bg-canvas">
              <LeftPanel
                viewMode={viewMode}
                contacts={contacts}
                selectedContactId={selectedContact?.id}
                onSelectContact={handleContactSelect}
                onSearch={setSearchQuery}
                onContactCreated={handleContactCreated}
                searchQuery={searchQuery}
                isLoadingContacts={isLoadingContacts}
                activeCalls={activeCalls}
                queuedCalls={queuedCalls}
                onSelectCall={handleCallSelect}
                onTakeCall={handleTakeCall}
                isLoadingCalls={isLoadingCalls}
                isLoadingQueue={isLoadingQueue}
                queueConfigs={queueConfigs}
              />
            </div>

            {/* Right Panel - 360 Contact Detail */}
            <div className="flex-1 bg-canvas overflow-hidden">
              {isLoadingContactDetail && !selectedContact ? (
                <ContactDetailSkeleton />
              ) : selectedContact ? (
                (() => {
                  const activeCallForContact = [...activeCalls, ...queuedCalls].find(c => {
                    if (c.contact_id === selectedContact.id || (c as any).contactId === selectedContact.id) {
                      return true;
                    }
                    if (c.direction === 'outbound') {
                      return (c as any).destination === selectedContact.phone;
                    }
                    return c.from_number === selectedContact.phone ||
                           (c as any).fromNumber === selectedContact.phone ||
                           c.phoneNumber === selectedContact.phone;
                  });
                  return (
                    <ContactDetailView
                      contact={selectedContact}
                      onContactUpdate={handleContactUpdate}
                      onContactDelete={handleContactDelete}
                      activeCallForContact={activeCallForContact}
                      liveSentiment={activeCallForContact?.id ? liveSentimentMap[activeCallForContact.id] || null : null}
                    />
                  );
                })()
              ) : viewMode === 'supervisor' ? (
                <DashboardCharts activeCalls={activeCalls} queuedCalls={queuedCalls} />
              ) : (
                <EmptyStage viewMode={viewMode} />
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Empty-stage screen shown when no contact/call is selected.
 * Quiet, atmospheric — dot-grid background, a tiny inline stat, editorial copy.
 * Better than a tiny centered icon.
 */
function EmptyStage({ viewMode }: { viewMode: ViewMode }) {
  const copy = {
    contacts: {
      kicker: 'Contacts',
      title: 'Waiting on you.',
      body: 'Pick someone on the left to see their history, sentiment timeline, and call controls. Or start a new contact.',
      Icon: Users,
    },
    calls: {
      kicker: 'Active calls',
      title: 'No live calls selected.',
      body: 'When a call is in progress, choose it from the left to open live transcription, controls, and context.',
      Icon: Phone,
    },
    queue: {
      kicker: 'Queue',
      title: 'Queue is clear.',
      body: 'When customers are waiting, they\u2019ll show up on the left — urgent calls first, sorted by the routing strategy of their queue.',
      Icon: ListTodo,
    },
    supervisor: { kicker: '', title: '', body: '', Icon: Users },
    settings:   { kicker: '', title: '', body: '', Icon: Users },
  }[viewMode] || { kicker: '', title: '', body: '', Icon: Users };

  return (
    <div className="relative h-full bg-dotgrid flex items-center justify-center px-10">
      <div className="max-w-md text-center animate-fade-up">
        <div className="kicker mb-4">{copy.kicker}</div>
        <h2 className="font-display text-[40px] leading-[1.05] text-ink mb-3 tracking-tightest">
          {copy.title}
        </h2>
        <p className="text-[14px] text-ink-muted leading-relaxed">
          {copy.body}
        </p>
      </div>
      {/* Corner crosshair — quiet operator-console marker */}
      <div className="absolute bottom-4 right-4 mono text-[9px] text-ink-faint uppercase tracking-[0.3em]">
        signalwire / cf
      </div>
    </div>
  );
}

export default UnifiedAgentDesktop;
