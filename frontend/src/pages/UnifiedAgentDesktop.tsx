import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { useCallFabricContext, ConnectedCustomer } from '../contexts/CallFabricContext';
import { useSocket } from '../hooks/useSocket';
import { UnifiedHeader } from '../components/unified/UnifiedHeader';
import { LeftPanel } from '../components/unified/LeftPanel';
import { IncomingCallBanner } from '../components/unified/IncomingCallBanner';
import { ContactDetailView } from '../components/contacts/ContactDetailView';
import { contactsApi, callsApi } from '../services/api';
import { Contact, ContactMinimal, Call } from '../types/callcenter';
import { Users } from 'lucide-react';
import { ContactDetailSkeleton } from '../components/shared/Skeleton';
import toast from 'react-hot-toast';
import { logger } from '../lib/logger';
import { mapCall, mapCalls } from '../lib/mapCall';

// View modes for the unified interface
export type ViewMode = 'contacts' | 'calls' | 'queue' | 'supervisor';

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
    return 'contacts';
  };

  // View state
  const [viewMode, setViewMode] = useState<ViewMode>(getInitialViewMode());

  // Agent stats
  const [stats, setStats] = useState({
    callsToday: 0,
    avgHandleTime: 0,
    fcr: 0,
    csat: 0,
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

    return () => {
      socket.off('call_update');
      socket.off('call_assigned');
      socket.off('call_ended');
      socket.off('contact_update');
      socket.off('agent_stats');
      socket.off('authenticated');
      socket.off('connect');
      socket.off('queue_update');
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

  // Initial data load
  useEffect(() => {
    loadContacts();
    loadActiveCalls();
    loadQueuedCalls();
    updateCallCounts();
  }, []);

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

  // Handle view mode change
  const handleViewModeChange = (mode: ViewMode) => {
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
        await callFabric.acceptCallAssignment();
        const contactId = callFabric.pendingCallAssignment.customerInfo?.contact_id ||
                         callFabric.pendingCallAssignment.customerInfo?.contactId;
        if (contactId) {
          navigate(`/contacts/${contactId}`);
          setViewMode('contacts');
        }
      } catch (error) {
        logger.error('Failed to accept call assignment:', error);
        toast.error('Failed to accept call');
      }
    }
  };

  return (
    <div className="h-screen flex flex-col bg-gray-900">
      {/* Incoming Call Banner - show for inbound calls */}
      {callFabric.callState === 'ringing' && callFabric.activeCall && callFabric.activeCall.direction === 'inbound' && (
        <IncomingCallBanner
          phoneNumber={callFabric.activeCall.callerId || 'Unknown'}
          onAnswer={() => handleAnswerIncoming(callFabric.activeCall?.callerId || '')}
          onDecline={handleDeclineIncoming}
        />
      )}

      {/* Call Assignment Banner - show when customer routed from queue */}
      {callFabric.pendingCallAssignment && !callFabric.isInConference && (
        <IncomingCallBanner
          phoneNumber={callFabric.pendingCallAssignment.callerNumber || callFabric.pendingCallAssignment.customerInfo?.phone || 'Unknown'}
          callerName={callFabric.pendingCallAssignment.customerInfo?.name}
          queueId={callFabric.pendingCallAssignment.queueId}
          aiContext={callFabric.pendingCallAssignment.context}
          onAnswer={handleAcceptAssignment}
          onDecline={callFabric.rejectCallAssignment}
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
        {/* Left Panel */}
        <div className="w-80 border-r border-gray-700 flex flex-col bg-gray-800">
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
          />
        </div>

        {/* Right Panel - 360 Contact Detail */}
        <div className="flex-1 bg-gray-900 overflow-hidden">
          {isLoadingContactDetail && !selectedContact ? (
            <ContactDetailSkeleton />
          ) : selectedContact ? (
            <ContactDetailView
              contact={selectedContact}
              onContactUpdate={handleContactUpdate}
              onContactDelete={handleContactDelete}
              activeCallForContact={
                [...activeCalls, ...queuedCalls].find(c => {
                  if (c.contact_id === selectedContact.id || (c as any).contactId === selectedContact.id) {
                    return true;
                  }
                  if (c.direction === 'outbound') {
                    return (c as any).destination === selectedContact.phone;
                  }
                  return c.from_number === selectedContact.phone ||
                         (c as any).fromNumber === selectedContact.phone ||
                         c.phoneNumber === selectedContact.phone;
                })
              }
            />
          ) : (
            <div className="h-full flex items-center justify-center text-gray-500">
              <div className="text-center">
                <Users className="w-16 h-16 mx-auto mb-4 opacity-50" />
                <p className="text-lg">
                  {viewMode === 'contacts' && 'Select a contact to view details'}
                  {viewMode === 'calls' && 'Select a call to view contact details'}
                  {viewMode === 'queue' && 'Select a queued call to view details'}
                  {viewMode === 'supervisor' && 'Select an agent or call to monitor'}
                </p>
                <p className="text-sm mt-2">
                  {viewMode === 'contacts' && 'Or create a new contact to get started'}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default UnifiedAgentDesktop;
