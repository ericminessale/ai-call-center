import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { useCallFabricContext, ConnectedCustomer } from '../contexts/CallFabricContext';
import { useSocket } from '../hooks/useSocket';
import { UnifiedHeader } from '../components/unified/UnifiedHeader';
import { DemoBanner } from '../components/shared/DemoBanner';
import { PhoneVerificationCard } from '../components/shared/PhoneVerificationCard';
import { useDemoLeaseHeartbeat } from '../hooks/useDemoLeaseHeartbeat';
import { useDemoVerification } from '../hooks/useDemoVerification';
import { useVerifyStore } from '../stores/verifyStore';
import { LeftPanel } from '../components/unified/LeftPanel';
import { IncomingCallBanner } from '../components/unified/IncomingCallBanner';
import { SettingsPanel } from '../components/unified/SettingsPanel';
import { ContactDetailView } from '../components/contacts/ContactDetailView';
import { contactsApi, callsApi, queueApi, callControlApi, callbacksApi, Callback } from '../services/api';
import { Contact, ContactMinimal, Call, QueueConfig } from '../types/callcenter';
import { CallbackDetail } from '../components/unified/CallbackDetail';
import type { SentimentData } from '../components/contacts/LiveCallTab';
import { Users, Phone, ListTodo } from 'lucide-react';
import { ContactDetailSkeleton } from '../components/shared/Skeleton';
import { DashboardCharts } from '../components/unified/DashboardCharts';
import { QueueDetailPanel } from '../components/unified/QueueDetailPanel';
import toast from 'react-hot-toast';
import { logger } from '../lib/logger';
import { mapCall, mapCalls } from '../lib/mapCall';

// View modes for the unified interface
export type ViewMode = 'contacts' | 'calls' | 'queue' | 'callbacks' | 'supervisor' | 'settings';

// Agent status options
export type AgentStatus = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

export function UnifiedAgentDesktop() {
  const { contactId, callId } = useParams<{ contactId?: string; callId?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuthStore();
  const runtimeConfig = useAuthStore((s) => s.runtimeConfig);
  const verifyHydrated = useVerifyStore((s) => s.hydrated);
  const isVerified = useVerifyStore((s) => s.verified);
  // Show the prominent verification card only for an UNVERIFIED demo
  // visitor (not platform operators; not until the status has hydrated, so
  // it doesn't flash for an already-verified returning visitor).
  const showVerifyCard =
    !!runtimeConfig?.demo_mode &&
    user != null && user.workspace_id != null &&
    verifyHydrated && !isVerified;

  // Hosted-demo only: keep the visitor's persona lease alive while
  // the dashboard is open, release it on tab close. No-op outside
  // demo mode.
  useDemoLeaseHeartbeat();
  // Hosted-demo only: hydrate + live-update the shared phone-verification
  // state consumed by the banner, the verification card, and the telephony
  // lock hints. No-op outside demo mode / for platform operators.
  useDemoVerification();

  // Determine initial view mode from URL
  const getInitialViewMode = (): ViewMode => {
    if (location.pathname.startsWith('/calls')) return 'calls';
    if (location.pathname.startsWith('/queue')) return 'queue';
    if (location.pathname.startsWith('/callbacks')) return 'callbacks';
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
  // Queue view: the WAITING call open in the bespoke queue detail panel (a
  // pre-answer triage surface that stays in the queue view, distinct from the
  // contact-centric ContactDetailView the other views navigate to).
  const [selectedQueuedCall, setSelectedQueuedCall] = useState<Call | null>(null);
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

  // Callback System (Tier 2r) — selected row + pending-count for header badge.
  const [selectedCallback, setSelectedCallback] = useState<Callback | null>(null);
  const [pendingCallbackCount, setPendingCallbackCount] = useState(0);
  // When the Contact banner deep-links into /callbacks, it passes a
  // suggested filter ('mine' / 'pending' / etc.) so the list lands on a
  // tab that contains the row. Cleared after the first apply so subsequent
  // filter clicks aren't overridden.
  const [callbacksForceFilter, setCallbacksForceFilter] = useState<'pending' | 'mine' | 'completed' | null>(null);

  // React-router state arrives via the location object — drain it on mount
  // (and on subsequent in-app navigations) into our local callback selection.
  // The Contact banner uses this to deep-link into the right callback row
  // with the right filter pre-applied.
  useEffect(() => {
    const state = (location.state as { callbackId?: number; suggestedFilter?: string } | null) ?? null;
    if (!state?.callbackId) return;
    let cancelled = false;
    callbacksApi
      .get(state.callbackId)
      .then((res) => {
        if (cancelled) return;
        setSelectedCallback(res.data.callback);
        if (
          state.suggestedFilter === 'mine' ||
          state.suggestedFilter === 'pending' ||
          state.suggestedFilter === 'completed'
        ) {
          setCallbacksForceFilter(state.suggestedFilter);
        }
        // Clear the location state so a later refresh doesn't re-apply.
        window.history.replaceState({}, '');
      })
      .catch((err) => logger.error('Failed to load deep-linked callback', err));
    return () => {
      cancelled = true;
    };
  }, [location.state]);

  // Live sentiment from AI agents, keyed by call ID
  const [liveSentimentMap, setLiveSentimentMap] = useState<Record<number, SentimentData>>({});

  // Call Fabric integration (shared context)
  const callFabric = useCallFabricContext();

  // Socket connection (proper authentication and reconnection handling)
  const socket = useSocket();

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
      if (!call) return;
      // FE-02 fix (2026-06-02 audit): call_assigned previously pushed the
      // raw backend object and skipped dedup. Two consequences:
      //   1. mapCall() never ran → the call lacked normalized
      //      status/handler_type/from_number fields, so ActiveCallsList's
      //      filter buckets (which key off status) dropped the agent's
      //      newly-taken call into uncategorizedCalls until a follow-up
      //      call_update arrived. Visible bucket-flicker on every Take.
      //   2. Socket reconnect could redeliver call_assigned, producing
      //      a duplicate row with the same id and a React key warning.
      // Match call_update's upsert pattern: map first, then find-and-replace
      // if the id already exists, otherwise append.
      const mappedCall = mapCall(call);
      setActiveCalls(prev => {
        const exists = prev.find(c => c.id === mappedCall.id);
        if (exists) return prev.map(c => c.id === mappedCall.id ? mappedCall : c);
        return [...prev, mappedCall];
      });
      if (mappedCall.contact_id) {
        loadContactDetail(mappedCall.contact_id);
      }
      updateCallCounts();
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
    // Load helpers are sampled by stable socket callbacks. Rebinding every
    // time search/contact state changes would duplicate live subscriptions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        setQueuedCalls(mapCalls(response.data.calls || []));
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
        aiActive: activeRes.data.calls?.filter((c) => c.status === 'ai_active').length || 0,
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
    // Mount-only bootstrap. Search changes have their own debounced contact
    // loader below and must not reload every dashboard data source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    // pathname is the route state that determines the view; the local helper
    // is recreated on render but should not itself retrigger navigation state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      case 'callbacks':
        navigate('/callbacks');
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

          const contactId = response.data.contact?.id;
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

  // Queue-view selection: open the bespoke queue detail panel WITHOUT
  // navigating away (the other views route a selected call to the contact).
  const handleQueueCallSelect = (call: Call) => setSelectedQueuedCall(call);

  // Drop the queue-panel selection once that call leaves the queue (taken,
  // ended, or routed to AI) so the panel never shows a stale caller.
  useEffect(() => {
    if (selectedQueuedCall && !queuedCalls.some((c) => c.id === selectedQueuedCall.id)) {
      setSelectedQueuedCall(null);
    }
  }, [queuedCalls, selectedQueuedCall]);

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
      if (response.data.contact?.id) {
        navigate(`/contacts/${response.data.contact.id}`);
      }
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

      const contactId = response.data.contact?.id;
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
            logger.debug('[Unified] Stopped monitoring before accepting assignment');
          } catch {
            // Ignore - may not have been monitoring
          }
        }
        await callFabric.acceptCallAssignment();
        const contactId = callFabric.pendingCallAssignment.customerInfo?.contactId;
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
    <div className="h-full flex flex-col bg-canvas">
      {/* Hosted-demo strip — renders only when DEMO_MODE=true on the
          backend. No-op in production-shape deployments. */}
      <DemoBanner />

      {/* Prominent verification card — only while an unverified demo visitor
          hasn't linked their phone; collapses to the banner's Verified badge
          on success. */}
      {showVerifyCard && <PhoneVerificationCard />}

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
        callbacksPending={pendingCallbackCount}
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
            {/* Left Panel — raised panel surface (matches the header); the main
                detail pane sits on the darker page bg for elevation contrast (spec
                rs-rail = --panel, rs-main = --page). */}
            <div className="w-[340px] border-r border-rule flex flex-col bg-canvas-raised">
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
                onSelectQueuedCall={handleQueueCallSelect}
                selectedQueuedCallId={selectedQueuedCall ? Number(selectedQueuedCall.id) : null}
                isLoadingCalls={isLoadingCalls}
                isLoadingQueue={isLoadingQueue}
                queueConfigs={queueConfigs}
                selectedCallbackId={selectedCallback?.id}
                onSelectCallback={(cb) => setSelectedCallback(cb)}
                onPendingCallbackCountChange={setPendingCallbackCount}
                callbacksForceFilter={callbacksForceFilter}
                onCallbacksFilterAck={() => setCallbacksForceFilter(null)}
              />
            </div>

            {/* Right Panel - 360 Contact Detail (or Callback detail when in callbacks view) */}
            <div className="flex-1 bg-canvas overflow-hidden">
              {viewMode === 'callbacks' && selectedCallback ? (
                <CallbackDetail
                  callback={selectedCallback}
                  currentUserId={user?.id ? Number(user.id) : -1}
                  onUpdated={(updated) => setSelectedCallback(updated)}
                  onClose={() => setSelectedCallback(null)}
                />
              ) : viewMode === 'queue' ? (
                /* Queue view owns its main pane: the bespoke triage panel for a
                   selected waiting call, else the empty state. Never the
                   contact-centric detail (that's for the other views). */
                selectedQueuedCall ? (
                  <QueueDetailPanel
                    call={selectedQueuedCall}
                    queueConfigs={queueConfigs}
                    onAnswer={() => handleTakeCall(selectedQueuedCall)}
                    onOpenContact={() => handleCallSelect(selectedQueuedCall)}
                  />
                ) : (
                  <EmptyStage viewMode="queue" />
                )
              ) : isLoadingContactDetail && !selectedContact ? (
                <ContactDetailSkeleton />
              ) : selectedContact ? (
                (() => {
                  // Filter all calls (active + queued) down to the ones tied
                  // to this contact. We then RANK among them — a bare
                  // ``.find()`` returns the first match in arbitrary order,
                  // which lets a stale/abandoned call shadow the agent's
                  // currently-connected one when both reference the same
                  // caller phone (real bug hit 2026-05-28: first call failed
                  // mid-flow, second call from same number → contact page
                  // showed the first call's transcript and the TEST button
                  // hit the dead SID instead of the live one).
                  const isMatch = (c: typeof activeCalls[number]) => {
                    if (c.contact_id === selectedContact.id || (c as any).contactId === selectedContact.id) {
                      return true;
                    }
                    if (c.direction === 'outbound') {
                      return (c as any).destination === selectedContact.phone;
                    }
                    return c.from_number === selectedContact.phone ||
                           (c as any).fromNumber === selectedContact.phone ||
                           c.phoneNumber === selectedContact.phone;
                  };
                  const matches = [...activeCalls, ...queuedCalls].filter(isMatch);

                  // Rank rules, highest priority first:
                  //   1. The agent's OWN currently-handled call (assigned to
                  //      them AND status is active/connecting). This is the
                  //      one they're actually on the phone with.
                  //   2. Any other active/connecting/ringing call for this
                  //      contact (e.g. supervisor view, another agent's
                  //      live call against this contact).
                  //   3. Queued pre-take calls (waiting/assigned/queued/urgent).
                  //   4. Any remaining match — last resort. Terminal-status
                  //      calls (ended/completed/failed) should have been
                  //      filtered out by the cleanup path, but if they leak
                  //      through they end up here, which is the right place.
                  // Among same-tier candidates, prefer the most recently
                  // created — new calls always win over older ones.
                  const myId = user?.id ? Number(user.id) : -1;
                  const LIVE = new Set(['active', 'connecting', 'ringing']);
                  const PRE_TAKE = new Set(['waiting', 'assigned', 'queued', 'urgent']);
                  const TERMINAL = new Set(['ended', 'completed', 'failed']);
                  const tier = (c: typeof matches[number]) => {
                    const status = c.status || '';
                    if (TERMINAL.has(status)) return 4;
                    if (c.assigned_agent_id === myId && LIVE.has(status)) return 0;
                    if (LIVE.has(status)) return 1;
                    if (PRE_TAKE.has(status)) return 2;
                    return 3;
                  };
                  const createdAt = (c: typeof matches[number]) => {
                    const raw = c.created_at || (c as any).createdAt;
                    return raw ? new Date(raw).getTime() : 0;
                  };
                  matches.sort((a, b) => {
                    const dt = tier(a) - tier(b);
                    if (dt !== 0) return dt;
                    return createdAt(b) - createdAt(a); // newest first
                  });
                  const activeCallForContact = matches[0];
                  return (
                    <ContactDetailView
                      contact={selectedContact}
                      onContactUpdate={handleContactUpdate}
                      onContactDelete={handleContactDelete}
                      activeCallForContact={activeCallForContact}
                      liveSentiment={activeCallForContact?.id ? liveSentimentMap[Number(activeCallForContact.id)] || null : null}
                    />
                  );
                })()
              ) : viewMode === 'supervisor' ? (
                <DashboardCharts activeCalls={activeCalls} queuedCalls={queuedCalls} onSelectCall={handleCallSelect} />
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
    callbacks: {
      kicker: 'Callbacks',
      title: 'No callback selected.',
      body: 'Pending callbacks live on the left. Claim one to lock it for yourself, then dial when ready.',
      Icon: Phone,
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
