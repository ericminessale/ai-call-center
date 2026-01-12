import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { useCallFabricContext } from '../contexts/CallFabricContext';
import { useSocket } from '../hooks/useSocket';
import { UnifiedHeader } from '../components/unified/UnifiedHeader';
import { LeftPanel } from '../components/unified/LeftPanel';
import { IncomingCallBanner } from '../components/unified/IncomingCallBanner';
import { ContactDetailView } from '../components/contacts/ContactDetailView';
import { contactsApi, callsApi } from '../services/api';
import { Contact, ContactMinimal, Call } from '../types/callcenter';
import { Users } from 'lucide-react';

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
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoadingContacts, setIsLoadingContacts] = useState(true);

  // Call state
  const [activeCalls, setActiveCalls] = useState<Call[]>([]);
  const [queuedCalls, setQueuedCalls] = useState<Call[]>([]);
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
    console.log('🔌 [UNIFIED] Setting up WebSocket listeners, socket exists:', !!socket, 'socket.id:', socket?.id, 'connected:', socket?.connected);
    if (!socket) {
      console.log('❌ [UNIFIED] No socket available, skipping listener setup');
      return;
    }

    // IMPORTANT: Register listeners even if not connected yet - they will work once connected
    // The socket.id check was causing a race condition where listeners were never registered
    console.log('✅ [UNIFIED] Registering listeners on socket (connected:', socket.connected, ')');

    // Remove any existing listeners first to prevent duplicates
    socket.off('call_update');
    socket.off('call_assigned');
    socket.off('call_ended');
    socket.off('agent_stats');
    socket.off('authenticated');
    socket.off('connect');

    // Handle socket connect/reconnect - reload data when connected
    socket.on('connect', () => {
      console.log('🔌 [UNIFIED] Socket connected event received, socket.id:', socket.id);
    });

    // Handle authentication success - reload data
    socket.on('authenticated', () => {
      console.log('🔐 [UNIFIED] Socket authenticated, reloading data');
      loadActiveCalls();
      loadQueuedCalls();
      updateCallCounts();
    });

    // Call updates - note: backend sends { call: callData }
    socket.on('call_update', (data: { call: Call }) => {
      console.log('📞 [UNIFIED] Received call_update:', data);
      const call = data.call;
      if (!call) {
        console.log('❌ [UNIFIED] No call in event data');
        return;
      }

      // Map backend fields and infer handler_type from status if not provided
      const mappedCall: Call = {
        ...call,
        from_number: call.from_number || (call as any).fromNumber,
        handler_type: call.handler_type || (call as any).handlerType || (call.status === 'ai_active' ? 'ai' : 'human'),
        phoneNumber: call.from_number || (call as any).fromNumber || call.phoneNumber || 'Unknown',
        // CRITICAL: Map signalwire_call_sid from various possible field names
        signalwire_call_sid: call.signalwire_call_sid || (call as any).signalwireCallSid || (call as any).signalwire_call_sid,
        call_sid: call.signalwire_call_sid || (call as any).signalwireCallSid || call.call_sid,
      };

      console.log('📞 [UNIFIED] Mapped call:', {
        id: mappedCall.id,
        status: mappedCall.status,
        handler_type: mappedCall.handler_type,
        phoneNumber: mappedCall.phoneNumber,
        signalwire_call_sid: mappedCall.signalwire_call_sid
      });

      // Check if call is ended/completed - remove from active list
      const isEnded = ['ended', 'completed'].includes(mappedCall.status);

      setActiveCalls(prev => {
        if (isEnded) {
          console.log('🏁 [UNIFIED] Removing ended call from list:', mappedCall.id);
          return prev.filter(c => c.id !== mappedCall.id);
        }

        const exists = prev.find(c => c.id === mappedCall.id);
        if (exists) {
          console.log('✏️ [UNIFIED] Updating existing call:', mappedCall.id);
          return prev.map(c => c.id === mappedCall.id ? mappedCall : c);
        }
        console.log('➕ [UNIFIED] Adding new call:', mappedCall.id, 'status:', mappedCall.status);
        return [...prev, mappedCall];
      });
      updateCallCounts();
    });

    // New call assigned to this agent
    socket.on('call_assigned', (data: { call: Call }) => {
      console.log('📞 [UNIFIED] Received call_assigned:', data);
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
      console.log('🏁 [UNIFIED] Received call_ended:', data);
      setActiveCalls(prev => prev.filter(c => c.id !== data.callId));
      updateCallCounts();
    });

    // Agent stats update
    socket.on('agent_stats', (newStats: typeof stats) => {
      console.log('📊 [UNIFIED] Received agent_stats:', newStats);
      setStats(newStats);
    });

    return () => {
      console.log('🧹 [UNIFIED] Cleaning up WebSocket listeners');
      socket.off('call_update');
      socket.off('call_assigned');
      socket.off('call_ended');
      socket.off('agent_stats');
      socket.off('authenticated');
      socket.off('connect');
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
      console.error('Failed to load contacts:', error);
    } finally {
      setIsLoadingContacts(false);
    }
  }, [searchQuery]);

  // Load contact detail
  const loadContactDetail = useCallback(async (id: number) => {
    try {
      const response = await contactsApi.get(id);
      setSelectedContact(response.data);
    } catch (error) {
      console.error('Failed to load contact:', error);
      setSelectedContact(null);
    }
  }, []);

  // Load active calls
  const loadActiveCalls = useCallback(async () => {
    try {
      console.log('📞 [UNIFIED] Loading active calls...');
      const response = await callsApi.list({ status: 'active,ai_active' });
      console.log('📞 [UNIFIED] Active calls response:', response.data);

      // Map backend fields to frontend format
      // API returns camelCase (fromNumber, handlerType, signalwireCallSid) while WebSocket uses snake_case
      const mappedCalls = (response.data.calls || []).map((call: any) => ({
        ...call,
        // Normalize to snake_case for consistency with WebSocket events
        from_number: call.fromNumber || call.from_number,
        handler_type: call.handlerType || call.handler_type || (call.status === 'ai_active' ? 'ai' : 'human'),
        phoneNumber: call.fromNumber || call.from_number || call.destination || 'Unknown',
        status: call.dashboard_status || call.status,
        // CRITICAL: Map signalwireCallSid (camelCase from API) to signalwire_call_sid (snake_case expected by frontend)
        signalwire_call_sid: call.signalwireCallSid || call.signalwire_call_sid,
        call_sid: call.signalwireCallSid || call.signalwire_call_sid || call.call_sid,
      }));

      console.log('📞 [UNIFIED] Mapped active calls:', mappedCalls.map((c: any) => ({
        id: c.id,
        phoneNumber: c.phoneNumber,
        status: c.status,
        handler_type: c.handler_type,
        signalwire_call_sid: c.signalwire_call_sid
      })));

      setActiveCalls(mappedCalls);
    } catch (error) {
      console.error('Failed to load active calls:', error);
    }
  }, []);

  // Load queued calls
  const loadQueuedCalls = useCallback(async () => {
    try {
      const response = await callsApi.list({ status: 'waiting' });
      setQueuedCalls(response.data.calls || []);
    } catch (error) {
      console.error('Failed to load queued calls:', error);
    }
  }, []);

  // Update call counts
  const updateCallCounts = useCallback(async () => {
    try {
      const [activeRes, queueRes] = await Promise.all([
        callsApi.list({ status: 'active,ai_active', per_page: 1 }),
        callsApi.list({ status: 'waiting', per_page: 1 }),
      ]);
      setCallCounts({
        active: activeRes.data.total || 0,
        queue: queueRes.data.total || 0,
        aiActive: activeRes.data.calls?.filter((c: Call) => c.status === 'ai_active').length || 0,
      });
    } catch (error) {
      console.error('Failed to update call counts:', error);
    }
  }, []);

  // Initial data load
  useEffect(() => {
    loadContacts();
    loadActiveCalls();
    loadQueuedCalls();
    updateCallCounts();
  }, []);

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
      // If we have a callId, find the contact from the call
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
    // Update URL to match view
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
    // Navigate back to contacts list
    navigate('/');
  };

  // Handle new contact created
  const handleContactCreated = (newContact: ContactMinimal) => {
    setContacts(prev => [newContact, ...prev]);
    navigate(`/contacts/${newContact.id}`);
  };

  // Handle call selection (from Active Calls view)
  const handleCallSelect = async (call: Call) => {
    console.log('📞 [UNIFIED] handleCallSelect:', {
      id: call.id,
      contact_id: call.contact_id,
      from_number: call.from_number,
      phoneNumber: call.phoneNumber
    });

    if (call.contact_id) {
      // Navigate to contact view
      await loadContactDetail(call.contact_id);
      navigate(`/contacts/${call.contact_id}`);
    } else {
      // Get phone number from various possible fields
      const phoneNumber = call.from_number || call.phoneNumber || (call as any).fromNumber;

      if (phoneNumber && phoneNumber !== 'Unknown' && phoneNumber !== 'unknown') {
        // Try to find or create contact by phone
        try {
          console.log('📞 [UNIFIED] Looking up or creating contact for:', phoneNumber);
          const response = await contactsApi.lookupOrCreate({
            phone: phoneNumber,
            displayName: phoneNumber,
          });
          console.log('📞 [UNIFIED] lookupOrCreate response:', response.data);

          // Response is { contact: Contact, created: boolean }
          const contactId = response.data.contact?.id || response.data.id;
          if (contactId) {
            // Reload contacts list to show the new contact
            loadContacts();
            navigate(`/contacts/${contactId}`);
          }
        } catch (error) {
          console.error('Failed to lookup/create contact:', error);
        }
      } else {
        console.warn('📞 [UNIFIED] No phone number available for call:', call.id);
      }
    }
    setViewMode('contacts');
  };

  // Handle take call from queue
  const handleTakeCall = async (call: Call) => {
    try {
      await callsApi.take(call.id);
      // Call will be assigned via WebSocket
      handleCallSelect(call);
    } catch (error) {
      console.error('Failed to take call:', error);
    }
  };

  // Handle incoming call answer
  const handleAnswerIncoming = async (phoneNumber: string) => {
    // Lookup or create contact
    try {
      const response = await contactsApi.lookupOrCreate({
        phone: phoneNumber,
        displayName: phoneNumber,
      });
      await callFabric.answerCall();
      navigate(`/contacts/${response.data.id}`);
      setViewMode('contacts');
    } catch (error) {
      console.error('Failed to handle incoming call:', error);
      // Still try to answer
      await callFabric.answerCall();
    }
  };

  // Handle incoming call decline
  const handleDeclineIncoming = async () => {
    await callFabric.hangup();
  };

  // Handle outbound call started from QuickDial - navigate to contact page
  const handleOutboundCallStarted = async (phoneNumber: string) => {
    console.log('📞 [UNIFIED] Outbound call started to:', phoneNumber);
    try {
      // Lookup or create contact for this phone number
      const response = await contactsApi.lookupOrCreate({
        phone: phoneNumber,
        displayName: phoneNumber,
      });
      console.log('📞 [UNIFIED] Contact lookup/create response:', response.data);

      // Get contact ID from response
      const contactId = response.data.contact?.id || response.data.id;
      if (contactId) {
        // Reload contacts list to show the new contact
        loadContacts();
        // Navigate to the contact page which will show the live call
        navigate(`/contacts/${contactId}`);
        setViewMode('contacts');
      }
    } catch (error) {
      console.error('Failed to lookup/create contact for outbound call:', error);
    }
  };

  return (
    <div className="h-screen flex flex-col bg-gray-900">
      {/* Incoming Call Banner - only show for inbound calls */}
      {callFabric.callState === 'ringing' && callFabric.activeCall && callFabric.activeCall.direction === 'inbound' && (
        <IncomingCallBanner
          phoneNumber={callFabric.activeCall.callerId || 'Unknown'}
          onAnswer={() => handleAnswerIncoming(callFabric.activeCall?.callerId || '')}
          onDecline={handleDeclineIncoming}
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
          />
        </div>

        {/* Right Panel - 360° Contact Detail */}
        <div className="flex-1 bg-gray-900 overflow-hidden">
          {selectedContact ? (
            <ContactDetailView
              contact={selectedContact}
              onContactUpdate={handleContactUpdate}
              onContactDelete={handleContactDelete}
              activeCallForContact={activeCalls.find(c =>
                c.contact_id === selectedContact.id ||
                c.from_number === selectedContact.phone ||
                (c as any).fromNumber === selectedContact.phone ||
                c.phoneNumber === selectedContact.phone
              )}
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
