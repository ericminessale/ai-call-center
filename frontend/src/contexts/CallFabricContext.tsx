import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useSocketContext } from './SocketContext';
import { conferencesApi, callsApi } from '../services/api';
import type { Conference, ConferenceParticipant } from '../types/callcenter';

// AI-collected context from the AI agent conversation
export interface AICollectedContext {
  customer_name?: string;
  reason?: string;  // General reason for call
  issue?: string;   // Support issue description
  urgency?: string;
  priority?: number;
  department?: string;
  interest?: string;  // Sales interest
  company?: string;
  budget?: string;
  error_message?: string;
  ai_summary?: string;
  source_agent?: string;
  preferred_handling?: 'ai' | 'human';
  queue?: string;
  global_data?: Record<string, any>;  // Raw global_data from AI
}

// Customer connected to agent's conference
export interface ConnectedCustomer {
  callId: string;
  callDbId?: number;
  callerNumber: string;
  queueId: string;
  conferenceName: string;
  customerInfo: {
    phone: string;
    name?: string;
    contact_id?: number;  // snake_case to match backend socket data
  };
  aiContext: AICollectedContext;
  connectedAt: Date;
}

declare global {
  interface Window {
    SignalWire: any;
  }
}

interface CallFabricClient {
  dial: (options: any) => Promise<any>;
  online: (options: any) => Promise<void>;
  offline: () => Promise<void>;
  registerDevice?: (options: any) => Promise<any>;
}

interface ActiveCall {
  id: string;
  callerId: string;
  direction: 'inbound' | 'outbound';
  status: string;
  startTime: Date;
  aiContext?: any;
  queueContext?: any;
  answer: () => Promise<void>;
  hangup: () => Promise<void>;
  hold: () => Promise<void>;
  unhold: () => Promise<void>;
  mute: () => Promise<void>;
  unmute: () => Promise<void>;
  sendDigits: (digits: string) => Promise<void>;
}

type AgentStatusType = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

interface CallFabricContextType {
  // Client state
  client: CallFabricClient | null;
  activeCall: ActiveCall | null;
  isOnline: boolean;
  isInitializing: boolean;
  isClientReady: boolean; // True when ICE gathering is complete and client is usable
  error: string | null;
  callState: 'idle' | 'ringing' | 'active' | 'ending';
  isMuted: boolean;
  micPermission: 'granted' | 'denied' | 'prompt' | 'unknown';

  // Agent status (unified with Call Fabric)
  agentStatus: AgentStatusType;
  isChangingStatus: boolean;
  conferenceJoinError: string | null;  // Error message if conference join failed

  // Conference state (for conference-based routing)
  agentConference: Conference | null;
  conferenceParticipants: ConferenceParticipant[];
  isInConference: boolean;

  // Connected customer (when customer joins agent's conference)
  connectedCustomer: ConnectedCustomer | null;
  onCustomerConnected?: (customer: ConnectedCustomer) => void;
  setOnCustomerConnected: (callback: ((customer: ConnectedCustomer) => void) | undefined) => void;
  clearConnectedCustomer: () => void;

  // Actions
  setAgentStatus: (status: AgentStatusType) => Promise<void>;
  initializeClient: () => Promise<void>;
  makeCall: (phoneNumber: string, context?: any) => Promise<any>;
  makeCallToSwml: (swmlUrl: string, context?: any) => Promise<any>;  // For takeover calls
  hangup: () => Promise<void>;
  answerCall: () => Promise<void>;
  requestMicPermission: () => Promise<boolean>;
  mute: () => Promise<void>;
  unmute: () => Promise<void>;
  hold: () => Promise<void>;
  unhold: () => Promise<void>;
  sendDigits: (digits: string) => Promise<void>;

  // Conference actions (per-interaction model)
  joinInteractionConference: (dialAddress: string, conferenceName: string) => Promise<void>;
  leaveConference: () => Promise<void>;

  // Pending call assignment (when customer routed but agent hasn't joined yet)
  pendingCallAssignment: CallAssignment | null;
  acceptCallAssignment: () => Promise<void>;
  acceptCallAssignmentWithData: (assignment: Partial<CallAssignment>) => Promise<void>;
  rejectCallAssignment: () => void;
}

// Call assignment from queue routing
// With server-initiated calls, the backend calls the agent directly.
// This event provides context about the incoming call.
export interface CallAssignment {
  callId: string;
  callDbId: number;
  callerNumber: string;
  queueId: string;
  context: any;
  agentId: number;
  agentName: string;
  conferenceName: string;
  agentCallSid?: string;  // The server-initiated call to the agent
  customerInfo: {
    phone: string;
    name?: string;
    contactId?: number;
  };
}

const CallFabricContext = createContext<CallFabricContextType | null>(null);

export function useCallFabricContext() {
  const context = useContext(CallFabricContext);
  if (!context) {
    throw new Error('useCallFabricContext must be used within a CallFabricProvider');
  }
  return context;
}

interface CallFabricProviderProps {
  children: ReactNode;
}

export function CallFabricProvider({ children }: CallFabricProviderProps) {
  const [client, setClient] = useState<CallFabricClient | null>(null);
  const [activeCall, setActiveCall] = useState<ActiveCall | null>(null);
  const [isOnline, setIsOnline] = useState(false);
  const [isInitializing, setIsInitializing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [callState, setCallState] = useState<'idle' | 'ringing' | 'active' | 'ending'>('idle');
  const [isMuted, setIsMuted] = useState(false);
  const [micPermission, setMicPermission] = useState<'granted' | 'denied' | 'prompt' | 'unknown'>('unknown');

  // Agent status state - check sessionStorage for persisted status
  // sessionStorage persists across page refreshes but clears when tab/browser closes
  // This means: new session = offline, page refresh = restore previous status
  const getInitialStatus = (): AgentStatusType => {
    const persisted = sessionStorage.getItem('agent_status');
    if (persisted === 'available') {
      console.log('📦 [CallFabric] Found persisted status in sessionStorage: available');
      return 'available'; // Will trigger auto-rejoin once client is ready
    }
    return 'offline';
  };
  const [agentStatus, setAgentStatusState] = useState<AgentStatusType>(getInitialStatus);
  const [isChangingStatus, setIsChangingStatus] = useState(false);
  const [conferenceJoinError, setConferenceJoinError] = useState<string | null>(null);

  // Conference state (for conference-based routing - always enabled)
  const [agentConference, setAgentConference] = useState<Conference | null>(null);
  const [conferenceParticipants, setConferenceParticipants] = useState<ConferenceParticipant[]>([]);
  const [isInConference, setIsInConference] = useState(false);
  const conferenceCallRef = useRef<any>(null);

  // Connected customer state (when customer joins agent's conference)
  const [connectedCustomer, setConnectedCustomer] = useState<ConnectedCustomer | null>(null);
  const onCustomerConnectedRef = useRef<((customer: ConnectedCustomer) => void) | undefined>(undefined);

  // Pending call assignment (customer waiting for agent to join interaction conference)
  const [pendingCallAssignment, setPendingCallAssignment] = useState<CallAssignment | null>(null);
  const pendingCallAssignmentRef = useRef<CallAssignment | null>(null);

  // Ref for activeCall to avoid stale closures in answerCall
  const activeCallRef = useRef<ActiveCall | null>(null);

  // Ref for leaveConference to avoid circular dependency in setAgentStatus
  const leaveAgentConferenceRef = useRef<() => Promise<void>>(() => Promise.resolve());

  // Refs for state values that need to be accessed in makeCall without stale closures
  const isInConferenceRef = useRef<boolean>(false);
  const setAgentStatusRef = useRef<(status: AgentStatusType) => Promise<void>>(() => Promise.resolve());
  const agentStatusRef = useRef<AgentStatusType>(getInitialStatus()); // Match initial state
  const isClientReadyRef = useRef<boolean>(false);
  const agentConferenceRef = useRef<Conference | null>(null);

  // ICE gathering readiness - client exists but may not be usable until ICE completes (~10s)
  const [isClientReady, setIsClientReady] = useState(false);
  const pendingStatusRef = useRef<AgentStatusType | null>(null);
  const iceReadyTimerRef = useRef<NodeJS.Timeout | null>(null);
  const pendingAutoRejoinRef = useRef<boolean>(false);  // Track if we need to auto-rejoin when client is ready
  const hasAttemptedAutoRejoinRef = useRef<boolean>(false);  // Ensure auto-rejoin only runs once per session

  const { user } = useAuthStore();
  const { socket, connectionStatus } = useSocketContext();
  const rootElementRef = useRef<HTMLDivElement | null>(null);
  const inviteRef = useRef<any>(null);
  const initializingRef = useRef(false);

  // Get subscriber token from backend
  const getSubscriberToken = async () => {
    try {
      const response = await fetch('/api/fabric/token', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        },
        body: JSON.stringify({
          reference: user?.email || 'agent',
          application_id: import.meta.env.VITE_FABRIC_APPLICATION_ID
        })
      });

      if (!response.ok) {
        throw new Error('Failed to get subscriber token');
      }

      const data = await response.json();
      return data.token;
    } catch (error) {
      console.error('Error getting subscriber token:', error);
      throw error;
    }
  };

  // Load WebRTC adapter for cross-browser compatibility (like SDK example does)
  const loadWebRTCAdapter = () => {
    return new Promise<void>((resolve, reject) => {
      // Check if adapter is already loaded
      if ((window as any).adapter) {
        console.log('✅ WebRTC adapter already loaded');
        resolve();
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://webrtc.github.io/adapter/adapter-latest.js';
      script.async = true;
      script.onload = () => {
        console.log('✅ WebRTC adapter loaded');
        resolve();
      };
      script.onerror = (err) => {
        console.warn('⚠️ Failed to load WebRTC adapter, continuing anyway:', err);
        resolve(); // Don't reject - adapter is helpful but not required
      };
      document.head.appendChild(script);
    });
  };

  // Load SignalWire SDK - using @dev for latest Call Fabric features
  // Add cache buster to ensure we get the latest version
  const loadSignalWireSDK = async () => {
    // Load WebRTC adapter first (like SDK example does)
    await loadWebRTCAdapter();

    return new Promise((resolve, reject) => {
      if (window.SignalWire) {
        console.log('✅ SignalWire SDK already loaded');
        resolve(true);
        return;
      }
      const script = document.createElement('script');
      // Use stable @signalwire/client (no @dev) per SignalWire recommendation
      script.src = 'https://unpkg.com/@signalwire/client';
      script.async = true;
      script.onload = () => {
        console.log('✅ SignalWire SDK loaded (@signalwire/client stable)');
        resolve(true);
      };
      script.onerror = reject;
      document.head.appendChild(script);
    });
  };

  // Initialize Call Fabric client
  const initializeClient = async () => {
    if (initializingRef.current || client) return;
    initializingRef.current = true;
    setIsInitializing(true);
    setError(null);

    try {
      console.log('📱 [CallFabric] Initializing client...');

      if (!window.SignalWire) {
        await loadSignalWireSDK();
      }

      const { SignalWire: SWire } = window.SignalWire;
      const token = await getSubscriberToken();

      if (!rootElementRef.current) {
        // Create rootElement exactly like SDK example - simple empty div
        // The SDK example has: <div id="rootElement"></div> in the HTML
        rootElementRef.current = document.createElement('div');
        rootElementRef.current.id = 'rootElement';
        // Keep it simple like the SDK example - no special positioning
        // Just make it visible for debugging
        rootElementRef.current.style.cssText = 'width:320px;height:240px;background:#222;';
        document.body.appendChild(rootElementRef.current);
        console.log('📞 [CallFabric] Created rootElement for media (matching SDK example)');
      }

      const swClient = await SWire({
        token: token,
        host: import.meta.env.VITE_SIGNALWIRE_HOST,
        debug: { logWsTraffic: true },
        logLevel: 'debug'
      });

      setClient(swClient);
      console.log('✅ [CallFabric] Client initialized');

      // ICE gathering takes ~10 seconds before pooled connections are available
      // Wait before marking client as ready to prevent "No valid pooled connections available" errors
      // Note: This is a workaround - ideally the SDK would provide a "ready" event
      const ICE_READY_DELAY = 11000; // 11 seconds to be safe
      console.log(`⏳ [CallFabric] Waiting ${ICE_READY_DELAY/1000}s for ICE gathering...`);
      iceReadyTimerRef.current = setTimeout(() => {
        console.log('✅ [CallFabric] ICE gathering complete, client is now ready');
        setIsClientReady(true);
      }, ICE_READY_DELAY);

    } catch (error) {
      console.error('❌ [CallFabric] Failed to initialize:', error);
      setError('Failed to initialize phone system');
      initializingRef.current = false;
    } finally {
      setIsInitializing(false);
    }
  };

  // Go online in Call Fabric
  const goOnline = useCallback(async () => {
    if (!client || isOnline) return;

    try {
      console.log('📱 [CallFabric] Going online...');

      await client.online({
        incomingCallHandlers: {
          all: async (notification: any) => {
            console.log('📞 [CallFabric] Incoming call notification:', notification);
            console.log('📞 [CallFabric] Incoming invite object:', notification.invite);
            console.log('📞 [CallFabric] Incoming invite details:', JSON.stringify(notification.invite?.details, null, 2));
            console.log('📞 [CallFabric] Incoming callID:', notification.invite?.details?.callID);
            inviteRef.current = notification.invite;

            const aiContext = notification.invite.details?.userVariables?.ai_context;
            const queueContext = notification.invite.details?.userVariables?.queue_context;

            setCallState('ringing');

            const incomingCall: ActiveCall = {
              id: notification.invite.details?.callID || '',
              callerId: notification.invite.details?.from || 'Unknown',
              direction: 'inbound',
              status: 'ringing',
              startTime: new Date(),
              aiContext,
              queueContext,
              answer: async () => {
                // Match SDK example exactly - use notification.invite from closure
                console.log('📞 [CallFabric] answer() called');
                console.log('📞 [CallFabric] notification.invite:', notification.invite);
                console.log('📞 [CallFabric] notification.invite.details:', notification.invite?.details);
                console.log('📞 [CallFabric] callID:', notification.invite?.details?.callID);
                console.log('📞 [CallFabric] from:', notification.invite?.details?.from);
                console.log('📞 [CallFabric] rootElement:', rootElementRef.current);

                try {
                  // Call accept() with audio-only (no video) since this is a voice call center
                  console.log('📞 [CallFabric] Calling notification.invite.accept() with audio-only...');
                  const call = await notification.invite.accept({
                    rootElement: rootElementRef.current,
                    audio: true,
                    video: false,  // Voice-only call - don't request camera
                  });

                  console.log('📞 [CallFabric] accept() returned:', call);
                  console.log('📞 [CallFabric] call.id:', call?.id);
                  console.log('📞 [CallFabric] call.state:', call?.state);
                  console.log('📞 [CallFabric] MISMATCH CHECK - invite callID:', notification.invite?.details?.callID, 'vs call.id:', call?.id);

                  // Set up comprehensive event handlers to debug
                  if (call) {
                    // Store globally for debugging
                    (window as any).__swCall = call;
                    (window as any).__swInvite = notification.invite;

                    call.on('destroy', () => {
                      console.warn('📞 [CallFabric] Inbound call destroyed');
                      setCallState('idle');
                      setActiveCall(null);
                      activeCallRef.current = null;
                    });

                    // Add more event listeners to debug
                    call.on('call.state', (state: any) => {
                      console.log('📞 [CallFabric] call.state event:', state);
                    });

                    call.on('room.joined', (params: any) => {
                      console.log('📞 [CallFabric] room.joined event:', params);
                    });

                    call.on('media.connected', () => {
                      console.log('📞 [CallFabric] media.connected event');
                    });

                    call.on('media.disconnected', () => {
                      console.log('📞 [CallFabric] media.disconnected event');
                    });
                  }

                  setCallState('active');
                  return call;
                } catch (acceptError) {
                  console.error('❌ [CallFabric] accept() threw error:', acceptError);
                  console.error('❌ [CallFabric] Error details:', JSON.stringify(acceptError, null, 2));
                  throw acceptError;
                }
              },
              hangup: async () => {
                await notification.invite.reject();
                setCallState('idle');
              },
              hold: async () => {},
              unhold: async () => {},
              mute: async () => {},
              unmute: async () => {},
              sendDigits: async () => {}
            };

            setActiveCall(incomingCall);
            activeCallRef.current = incomingCall;  // Update ref for answerCall
            console.log('📞 [CallFabric] ActiveCall set:', incomingCall.id);

            const autoAnswer = localStorage.getItem('auto_answer') === 'true';
            if (autoAnswer) {
              console.log('📞 [CallFabric] Auto-answer enabled, answering in 1s...');
              setTimeout(() => incomingCall.answer(), 1000);
            }
          }
        }
      });

      setIsOnline(true);
      console.log('✅ [CallFabric] Now online and ready to receive calls');

    } catch (error) {
      console.error('❌ [CallFabric] Failed to go online:', error);
      setError('Failed to go online');
      throw error;
    }
  }, [client, isOnline]);

  // Go offline in Call Fabric
  const goOffline = useCallback(async () => {
    if (!client || !isOnline) return;

    try {
      console.log('📱 [CallFabric] Going offline...');
      await client.offline();
      setIsOnline(false);
      console.log('✅ [CallFabric] Now offline');
    } catch (error) {
      console.error('❌ [CallFabric] Failed to go offline:', error);
      throw error;
    }
  }, [client, isOnline]);

  // Update Redis status via socket
  const updateRedisStatus = useCallback((status: AgentStatusType) => {
    if (!socket) {
      console.log('❌ [CallFabric] No socket for Redis update');
      return;
    }

    const token = localStorage.getItem('access_token');
    if (token) {
      console.log('📤 [CallFabric] Updating Redis status:', status);
      socket.emit('set_agent_status', { token, status });
    }
  }, [socket]);

  // UNIFIED: Set agent status (controls both Call Fabric and Redis)
  const setAgentStatus = useCallback(async (newStatus: AgentStatusType) => {
    console.log('🔄 [CallFabric] setAgentStatus called:', newStatus);
    console.log('  - client:', !!client, 'isClientReady:', isClientReady, 'isOnline:', isOnline);
    console.log('  - socket:', !!socket, 'connected:', connectionStatus);

    // If client isn't ready yet (ICE gathering in progress), queue the status change
    if (!client || !isClientReady) {
      if (newStatus === 'available') {
        console.log('⏳ [CallFabric] Client not ready, queuing status change:', newStatus);
        pendingStatusRef.current = newStatus;
        setIsChangingStatus(true); // Show "connecting" state in UI
        setError(null);
        return;
      } else {
        // For offline/break, just update Redis without Call Fabric
        console.log('📤 [CallFabric] Client not ready, updating Redis only for:', newStatus);
        updateRedisStatus(newStatus);
        setAgentStatusState(newStatus);
        pendingStatusRef.current = null; // Clear any pending "available" request
        return;
      }
    }

    setIsChangingStatus(true);
    setError(null);

    try {
      // Handle Call Fabric online/offline based on status
      // NEW: Per-interaction conference model
      // - Agent goes online/offline via Call Fabric (for receiving inbound calls)
      // - Agent does NOT join a conference when going available
      // - When a call is assigned, agent receives 'call_assignment' socket event
      // - Agent then dials into the interaction conference
      if (newStatus === 'available') {
        if (!isOnline) {
          await goOnline();
        }
        // No conference join - agent just becomes available to receive call assignments
        console.log('✅ [CallFabric] Agent available - ready to receive call assignments');
      } else if (newStatus === 'offline' || newStatus === 'break') {
        if (isOnline) {
          await goOffline();
        }
        // Leave any active conference
        if (isInConference) {
          try {
            await leaveAgentConferenceRef.current();
          } catch (confError) {
            console.error('⚠️ [CallFabric] Failed to leave conference:', confError);
          }
        }
        // Clear any pending call assignment
        setPendingCallAssignment(null);
      }
      // 'busy' and 'after-call' keep Call Fabric online but remove from queue

      // Update Redis status
      updateRedisStatus(newStatus);

      // Persist to sessionStorage for auto-restore on refresh (clears when tab closes)
      sessionStorage.setItem('agent_status', newStatus);

      // Update local state
      setAgentStatusState(newStatus);
      setConferenceJoinError(null);
      console.log('✅ [CallFabric] Status changed to:', newStatus);

    } catch (error: any) {
      console.error('❌ [CallFabric] Failed to change status:', error);
      const errorMsg = error?.message || 'Failed to change status';
      setError(errorMsg);
      setConferenceJoinError(errorMsg);

      // If going available failed, revert to offline
      if (newStatus === 'available') {
        console.log('⚠️ [CallFabric] Reverting to offline due to error');
        setAgentStatusState('offline');
        sessionStorage.setItem('agent_status', 'offline');
        updateRedisStatus('offline');
      }
    } finally {
      setIsChangingStatus(false);
    }
  }, [client, isClientReady, isOnline, isInConference, socket, connectionStatus, goOnline, goOffline, updateRedisStatus]);

  // Request microphone permission
  const requestMicPermission = useCallback(async (): Promise<boolean> => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());
      setMicPermission('granted');
      return true;
    } catch (err: any) {
      setMicPermission('denied');
      setError(`Microphone error: ${err.message}`);
      return false;
    }
  }, []);

  // Make outbound call
  // If agent is in conference (available), use server-side dial-out to add to conference
  // If agent is offline, auto-switch to available first, then dial-out
  const makeCall = useCallback(async (phoneNumber: string, context?: any) => {
    const token = localStorage.getItem('access_token');

    // Helper function to dial out via backend API (joins call to agent's conference)
    // NOTE: We do NOT set callState here because the call is server-initiated.
    // Call state updates will come via Socket.IO 'call_update' events which update
    // the activeCallForContact prop in the UI components.
    const dialOutToConference = async (conferenceName: string, contactId?: number) => {
      console.log('📞 [CallFabric] Dial-out via conference:', conferenceName);

      const response = await fetch(`/api/conferences/${conferenceName}/dial-out`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          phone_number: phoneNumber,
          contact_id: contactId,
          context: context
        })
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || 'Failed to dial out');
      }

      const data = await response.json();
      console.log('✅ [CallFabric] Dial-out initiated:', data);

      // The call is being placed by the server, not the browser
      // The agent is already in the conference and will hear the call when answered
      // Set callState to 'ringing' for immediate UI feedback
      // The call_update socket event will provide further updates
      setCallState('ringing');

      return data;
    };

    // Helper to wait for client readiness (ICE gathering)
    const waitForClientReady = async (maxWaitMs: number = 15000): Promise<boolean> => {
      const startTime = Date.now();
      while (!isClientReadyRef.current && (Date.now() - startTime) < maxWaitMs) {
        console.log('⏳ [CallFabric] Waiting for client to be ready...');
        await new Promise(r => setTimeout(r, 500));
      }
      return isClientReadyRef.current;
    };

    try {
      // Use refs for current state values (avoids stale closures)
      const currentIsInConference = isInConferenceRef.current;
      const currentAgentConference = agentConferenceRef.current;
      const currentAgentStatus = agentStatusRef.current;

      console.log('📞 [CallFabric] makeCall called:', {
        phoneNumber,
        currentIsInConference,
        currentAgentStatus,
        currentAgentConference: currentAgentConference?.conferenceName,
        isClientReady: isClientReadyRef.current
      });

      // Case 1: Agent has a conference (available status) - use server-side dial-out
      // Note: We check agentConference, not isInConference, because the dial-out API
      // doesn't require the browser WebRTC connection to be active - it just needs
      // the conference name to exist on the server side.
      if (currentAgentConference && (currentAgentStatus === 'available' || currentIsInConference)) {
        console.log('📞 [CallFabric] Case 1: Agent has conference, using dial-out API');
        const result = await dialOutToConference(
          currentAgentConference.conferenceName,
          context?.contact_id
        );
        return result;
      }

      // Case 2: Agent is offline - auto-go-available first, then dial-out
      if (currentAgentStatus === 'offline') {
        console.log('📞 [CallFabric] Case 2: Agent offline, auto-switching to available...');
        setIsChangingStatus(true);

        try {
          // If client isn't ready yet (ICE gathering), wait for it first
          if (!isClientReadyRef.current) {
            console.log('⏳ [CallFabric] Client not ready, waiting for ICE gathering...');
            const clientReady = await waitForClientReady(15000);
            if (!clientReady) {
              throw new Error('Phone system still initializing - please wait and try again');
            }
            console.log('✅ [CallFabric] Client is now ready');
          }

          // Now go available (this will join the conference)
          console.log('📞 [CallFabric] Calling setAgentStatus(available)...');
          await setAgentStatusRef.current('available');

          // Wait for conference join to complete (poll isInConference)
          let attempts = 0;
          const maxAttempts = 40; // 20 seconds max (conference join can take time with retries)
          while (!isInConferenceRef.current && attempts < maxAttempts) {
            await new Promise(r => setTimeout(r, 500));
            attempts++;
            if (attempts % 10 === 0) {
              console.log(`⏳ [CallFabric] Still waiting for conference join... (${attempts * 500 / 1000}s)`);
            }
          }

          if (!isInConferenceRef.current) {
            throw new Error('Failed to join conference - please try again');
          }

          console.log('✅ [CallFabric] Now in conference, dialing out...');

          // Now dial out - use the conference name from ref (should be set by now)
          const confName = agentConferenceRef.current?.conferenceName || `agent-conf-${user?.id}`;
          const result = await dialOutToConference(confName, context?.contact_id);
          return result;

        } finally {
          setIsChangingStatus(false);
        }
      }

      // Case 3: Agent is in some other state (busy, break, after-call)
      // This is an error state - should not make outbound calls in this mode
      console.error('❌ [CallFabric] Case 3: Invalid state for outbound call:', {
        status: currentAgentStatus,
        isInConference: currentIsInConference,
        hasConference: !!currentAgentConference,
        conferenceName: currentAgentConference?.conferenceName
      });

      throw new Error(`Cannot make outbound call while in ${currentAgentStatus} status. Please go available first.`);

    } catch (error: any) {
      console.error('❌ [CallFabric] Failed to make call:', error);
      setError(error?.message || 'Failed to make call');
      setCallState('idle');
      throw error; // Re-throw so caller knows it failed
    }
  }, [user]); // Minimal dependencies - use refs for everything else

  // Make call to SWML URL (for takeover calls)
  const makeCallToSwml = useCallback(async (swmlUrl: string, context?: any) => {
    if (!client) {
      setError('Phone system not initialized');
      return;
    }

    if (!isClientReady) {
      setError('Phone system still initializing, please wait a moment');
      return;
    }

    try {
      console.log('📞 [CallFabric] Making SWML call to:', swmlUrl);
      setCallState('ringing');

      // When dialing a URL, SignalWire fetches the SWML and executes it
      const call = await client.dial({
        to: swmlUrl,
        rootElement: rootElementRef.current,
        audio: true,
        video: false,  // Voice-only - don't request camera
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user?.id,
          agent_name: user?.name,
          call_type: 'takeover',
          ...context
        }
      });

      call.on('call.state', (state: any) => {
        console.log('📞 [CallFabric] SWML call state:', state);
        if (state === 'active' || state === 'answered') setCallState('active');
        else if (state === 'ending' || state === 'ended') setCallState('ending');
      });

      call.on('destroy', () => {
        console.log('📞 [CallFabric] SWML call destroyed');
        setActiveCall(null);
        setCallState('idle');
      });

      const takeoverCall: ActiveCall = {
        id: call.id,
        callerId: 'Takeover Call',
        direction: 'outbound',
        status: 'connecting',
        startTime: new Date(),
        answer: async () => {},
        hangup: async () => await call.hangup(),
        hold: async () => {},
        unhold: async () => {},
        mute: async () => await call.audioMute(),
        unmute: async () => await call.audioUnmute(),
        sendDigits: async (digits: string) => await call.sendDigits(digits)
      };

      setActiveCall(takeoverCall);
      await call.start();
      console.log('✅ [CallFabric] SWML call started');
      return call;

    } catch (error) {
      console.error('❌ [CallFabric] Failed to make SWML call:', error);
      setError('Failed to take over call');
      setCallState('idle');
    }
  }, [client, isClientReady, user]);

  // Hang up current call
  const hangup = useCallback(async () => {
    if (!activeCall) return;
    try {
      await activeCall.hangup();
      setActiveCall(null);
      setCallState('idle');
    } catch (error) {
      console.error('Failed to hang up:', error);
    }
  }, [activeCall]);

  // Answer incoming call
  // When agent answers a server-initiated call, SignalWire fetches the SWML URL
  // which joins the agent to the conference automatically
  const answerCall = useCallback(async () => {
    const currentCall = activeCallRef.current;

    console.log('📞 [CallFabric] answerCall called');
    console.log('📞 [CallFabric] activeCall (from ref):', currentCall?.id);

    if (!currentCall || currentCall.direction !== 'inbound') {
      console.log('⚠️ [CallFabric] No inbound call to answer');
      return;
    }

    try {
      // Ensure microphone permission before accepting - this triggers the browser prompt
      // if permission hasn't been granted yet
      console.log('📞 [CallFabric] Requesting microphone permission...');
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(track => track.stop()); // Release immediately
        console.log('✅ [CallFabric] Microphone permission granted');
      } catch (micError) {
        console.error('❌ [CallFabric] Microphone permission denied:', micError);
        setError('Microphone access required to answer calls');
        return;
      }

      console.log('📞 [CallFabric] Accepting incoming call...');
      const call = await currentCall.answer();
      console.log('✅ [CallFabric] Call answered, call object:', call);

      setCallState('active');

      // If we have a pending assignment, set up conference tracking
      const assignment = pendingCallAssignmentRef.current;
      if (assignment) {
        console.log('📞 [CallFabric] Setting conference state from assignment:', assignment.conferenceName);
        const conferenceInfo: Conference = {
          id: 0,
          conferenceName: assignment.conferenceName,
          conferenceType: 'interaction',
          ownerUserId: user?.id || 0,
          status: 'active',
          createdAt: new Date().toISOString()
        };
        setAgentConference(conferenceInfo);
        agentConferenceRef.current = conferenceInfo;
        setIsInConference(true);
        isInConferenceRef.current = true;

        // Clear the pending assignment
        setPendingCallAssignment(null);
        pendingCallAssignmentRef.current = null;
      }
    } catch (error) {
      console.error('❌ [CallFabric] Failed to answer call:', error);
    }
  }, [user]);

  // Join an interaction conference (per-interaction model)
  // Called when agent accepts a call assignment - dials into the interaction conference
  // where the customer is already waiting
  const joinInteractionConference = useCallback(async (dialAddress: string, conferenceName: string) => {
    if (!client || !user) {
      console.log('⚠️ [CallFabric] Cannot join conference - client or user not ready');
      throw new Error('Client or user not ready');
    }

    if (isInConference) {
      console.log('⚠️ [CallFabric] Already in a conference');
      throw new Error('Already in a conference');
    }

    try {
      console.log('📞 [CallFabric] Joining interaction conference:', conferenceName);
      console.log('📞 [CallFabric] Dialing:', dialAddress);

      // Dial the resource address (e.g., /public/join-conference?conf=interaction-abc123&agent_id=4)
      const call = await client.dial({
        to: dialAddress,
        rootElement: rootElementRef.current,
        audio: true,
        video: false,  // Voice-only - don't request camera
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user.id,
          call_type: 'interaction',
          conference_name: conferenceName
        }
      });

      // Set conference info BEFORE starting the call
      const conferenceInfo: Conference = {
        id: 0, // Will be updated by status callback
        conferenceName: conferenceName,
        conferenceType: 'interaction',
        ownerUserId: user.id,
        status: 'active',
        createdAt: new Date().toISOString()
      };
      setAgentConferenceSync(conferenceInfo);

      call.on('call.state', (state: any) => {
        console.log('📞 [CallFabric] Interaction conference call state:', state);
        if (state === 'active' || state === 'answered') {
          setIsInConferenceSync(true);
          setConferenceJoinError(null);
          // Clear pending assignment since we're now connected
          setPendingCallAssignment(null);
        } else if (state === 'ending' || state === 'ended') {
          setIsInConferenceSync(false);
          conferenceCallRef.current = null;
        }
      });

      call.on('destroy', () => {
        console.log('📞 [CallFabric] Interaction conference call destroyed');
        setIsInConferenceSync(false);
        conferenceCallRef.current = null;
      });

      conferenceCallRef.current = call;
      await call.start();

      // Join socket room for conference updates
      if (socket) {
        const token = localStorage.getItem('access_token');
        socket.emit('join_conference', { conference_name: conferenceName, token });
      }

      console.log('✅ [CallFabric] Joined interaction conference:', conferenceName);

    } catch (error) {
      console.error('❌ [CallFabric] Failed to join interaction conference:', error);
      setError('Failed to join conference');
      throw error;
    }
  }, [client, user, isInConference, socket]);

  // Leave current conference (works for both agent and interaction conferences)
  const leaveConference = useCallback(async () => {
    if (!isInConference || !conferenceCallRef.current) {
      return;
    }

    try {
      console.log('📞 [CallFabric] Leaving conference...');

      // Leave socket room first
      if (socket && agentConference?.conferenceName) {
        socket.emit('leave_conference', { conference_name: agentConference.conferenceName });
      }

      await conferenceCallRef.current.hangup();
      // Update refs synchronously to prevent race conditions
      isInConferenceRef.current = false;
      agentConferenceRef.current = null;
      setIsInConference(false);
      setAgentConference(null);
      setConferenceParticipants([]);
      conferenceCallRef.current = null;
      console.log('✅ [CallFabric] Left conference');
    } catch (error) {
      console.error('❌ [CallFabric] Failed to leave conference:', error);
      // Still clear state even if hangup fails
      isInConferenceRef.current = false;
      agentConferenceRef.current = null;
      setIsInConference(false);
      setAgentConference(null);
      setConferenceParticipants([]);
      conferenceCallRef.current = null;
    }
  }, [isInConference, socket, agentConference]);

  // Alias for backward compatibility
  const leaveAgentConference = leaveConference;

  // Accept a call assignment - dial OUT to join the conference
  // The backend sends a socket notification (not a call) when a customer is routed.
  // Agent clicks Accept, and we dial OUT to join the conference.
  // This avoids the SignalWire SDK bug where inbound call answering fails due to connection pooling.
  const acceptCallAssignment = useCallback(async () => {
    if (!pendingCallAssignment) {
      console.log('⚠️ [CallFabric] No pending call assignment to accept');
      return;
    }

    if (!client || !user) {
      console.log('⚠️ [CallFabric] Cannot accept assignment - client or user not ready');
      return;
    }

    const { conferenceName, callDbId } = pendingCallAssignment;
    console.log('📞 [CallFabric] Accepting call assignment via DIAL-OUT:', conferenceName);

    try {
      // Step 1: Prepare the join by storing params in Redis (more reliable than query params)
      console.log('📞 [CallFabric] Preparing conference join via API...');
      const prepareResponse = await conferencesApi.prepareJoin({
        agent_id: user.id,
        conference_name: conferenceName,
        call_id: callDbId
      });

      const dialAddress = prepareResponse.data.dial_address;
      console.log('📞 [CallFabric] Got dial address from API:', dialAddress);

      // Capture callDbId for status updates in event handlers
      const dbCallIdForHandlers = callDbId;

      // Step 2: Dial OUT to the conference resource with the token
      const call = await client.dial({
        to: dialAddress,
        rootElement: rootElementRef.current,
        audio: true,
        video: false,  // Voice-only - don't request camera
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user.id,
          call_type: 'interaction',
          conference_name: conferenceName,
          token: prepareResponse.data.token
        }
      });

      // Helper to mark call as connected
      let hasMarkedActive = false;
      const markCallActive = async () => {
        if (hasMarkedActive) return;
        hasMarkedActive = true;
        console.log('✅ [CallFabric] Marking call as ACTIVE');
        setCallState('active');
        // Update backend call status to 'active'
        if (dbCallIdForHandlers) {
          try {
            await callsApi.updateStatus(dbCallIdForHandlers, 'active');
            console.log('✅ [CallFabric] Updated call status to active in backend');
          } catch (err) {
            console.error('❌ [CallFabric] Failed to update call status:', err);
          }
        }
      };

      // Set up call event handlers
      call.on('call.state', async (state: any) => {
        console.log('📞 [CallFabric] Conference call state:', state);
        // Check for various "connected" states - SignalWire may use different values
        const connectedStates = ['active', 'answered', 'answering', 'early', 'trying'];
        if (connectedStates.includes(state)) {
          await markCallActive();
        } else if (state === 'ended' || state === 'hangup' || state === 'destroy') {
          setCallState('idle');
          setActiveCall(null);
          activeCallRef.current = null;
          setIsInConference(false);
          isInConferenceRef.current = false;
          setConnectedCustomer(null);
          // Update backend call status to 'ended'
          if (dbCallIdForHandlers) {
            try {
              await callsApi.updateStatus(dbCallIdForHandlers, 'ended');
            } catch (err) {
              // Ignore - might already be ended
            }
          }
        }
      });

      // Also listen for media/connect events that indicate connection
      call.on('call.joined', async () => {
        console.log('📞 [CallFabric] Call joined event');
        await markCallActive();
      });

      call.on('call.updated', async (params: any) => {
        console.log('📞 [CallFabric] Call updated:', params);
        // If we get updated event, call is likely connected
        if (params?.state === 'active' || params?.node_id) {
          await markCallActive();
        }
      });

      call.on('destroy', async () => {
        console.log('📞 [CallFabric] Conference call destroyed');
        setCallState('idle');
        setActiveCall(null);
        activeCallRef.current = null;
        setIsInConference(false);
        isInConferenceRef.current = false;
        setConnectedCustomer(null);
        // Update backend if call had a DB ID
        if (dbCallIdForHandlers) {
          try {
            await callsApi.updateStatus(dbCallIdForHandlers, 'ended');
          } catch (err) {
            // Ignore
          }
        }
      });

      // Track the call
      setActiveCall(call);
      activeCallRef.current = call;
      conferenceCallRef.current = call;
      setCallState('ringing');

      // Set conference info for tracking
      const conferenceInfo: Conference = {
        id: 0,
        conferenceName: conferenceName,
        conferenceType: 'interaction',
        ownerUserId: user.id,
        status: 'active',
        createdAt: new Date().toISOString()
      };
      setAgentConference(conferenceInfo);
      agentConferenceRef.current = conferenceInfo;
      setIsInConference(true);
      isInConferenceRef.current = true;

      // Join the socket room for conference events
      if (socket) {
        const token = localStorage.getItem('access_token');
        socket.emit('join_conference', { conference_name: conferenceName, token });
      }

      // Start the call - this actually initiates the dial
      await call.start();

      // After call.start() completes, the call should be connected
      // Give it a moment then mark as active if not already
      setTimeout(async () => {
        if (!hasMarkedActive) {
          console.log('📞 [CallFabric] Fallback: marking call active after start() completed');
          await markCallActive();
        }
      }, 1000);

      // Set up connectedCustomer with AI context for UI display
      if (pendingCallAssignment.customerInfo) {
        const customer: ConnectedCustomer = {
          callId: pendingCallAssignment.callId || '',
          callDbId: pendingCallAssignment.callDbId,
          callerNumber: pendingCallAssignment.callerNumber || pendingCallAssignment.customerInfo.phone || '',
          queueId: pendingCallAssignment.queueId || '',
          conferenceName: conferenceName,
          customerInfo: {
            name: pendingCallAssignment.customerInfo.name,
            contact_id: pendingCallAssignment.customerInfo.contact_id || (pendingCallAssignment.customerInfo as any).contactId,
          },
          aiContext: pendingCallAssignment.context || {},
          connectedAt: new Date()
        };
        setConnectedCustomer(customer);
        console.log('📋 [CallFabric] Set connectedCustomer with AI context:', pendingCallAssignment.context);

        // Also call the callback if set (for navigation/additional handling)
        if (onCustomerConnectedRef.current) {
          onCustomerConnectedRef.current(customer);
        }
      }

      console.log('✅ [CallFabric] Dialing out to conference...');

      // Clear the pending assignment
      setPendingCallAssignment(null);
    } catch (error) {
      console.error('❌ [CallFabric] Failed to accept call assignment:', error);
      throw error;
    }
  }, [pendingCallAssignment, user, client, socket]);

  // Accept a call assignment with explicit data (for taking calls from queue)
  const acceptCallAssignmentWithData = useCallback(async (assignment: Partial<CallAssignment>) => {
    if (!assignment.conferenceName) {
      console.log('⚠️ [CallFabric] No conference name in assignment data');
      return;
    }

    if (!client || !user) {
      console.log('⚠️ [CallFabric] Cannot accept assignment - client or user not ready');
      return;
    }

    const conferenceName = assignment.conferenceName;
    console.log('📞 [CallFabric] Accepting call assignment with data via DIAL-OUT:', conferenceName);

    try {
      // Step 1: Prepare the join by storing params in Redis (more reliable than query params)
      console.log('📞 [CallFabric] Preparing conference join via API...');
      const prepareResponse = await conferencesApi.prepareJoin({
        agent_id: user.id,
        conference_name: conferenceName,
        call_id: assignment.callDbId
      });

      const dialAddress = prepareResponse.data.dial_address;
      console.log('📞 [CallFabric] Got dial address from API:', dialAddress);

      // Capture callDbId for status updates in event handlers
      const dbCallIdForStatusUpdate = assignment.callDbId;

      // Step 2: Dial OUT to the conference resource with the token
      const call = await client.dial({
        to: dialAddress,
        rootElement: rootElementRef.current,
        audio: true,
        video: false,
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user.id,
          call_type: 'interaction',
          conference_name: conferenceName,
          token: prepareResponse.data.token
        }
      });

      // Helper to mark call as connected
      let hasMarkedActive = false;
      const markCallActive = async () => {
        if (hasMarkedActive) return;
        hasMarkedActive = true;
        console.log('✅ [CallFabric] Marking call as ACTIVE (with data)');
        setCallState('active');
        // Update backend call status to 'active'
        if (dbCallIdForStatusUpdate) {
          try {
            await callsApi.updateStatus(dbCallIdForStatusUpdate, 'active');
            console.log('✅ [CallFabric] Updated call status to active in backend');
          } catch (err) {
            console.error('❌ [CallFabric] Failed to update call status:', err);
          }
        }
      };

      // Set up call event handlers
      call.on('call.state', async (state: any) => {
        console.log('📞 [CallFabric] Conference call state:', state);
        // Check for various "connected" states - SignalWire may use different values
        const connectedStates = ['active', 'answered', 'answering', 'early', 'trying'];
        if (connectedStates.includes(state)) {
          await markCallActive();
        } else if (state === 'ended' || state === 'hangup' || state === 'destroy') {
          setCallState('idle');
          setActiveCall(null);
          activeCallRef.current = null;
          setIsInConference(false);
          isInConferenceRef.current = false;
          setConnectedCustomer(null);
          // Update backend call status to 'ended'
          if (dbCallIdForStatusUpdate) {
            try {
              await callsApi.updateStatus(dbCallIdForStatusUpdate, 'ended');
            } catch (err) {
              // Ignore - might already be ended
            }
          }
        }
      });

      // Also listen for media/connect events that indicate connection
      call.on('call.joined', async () => {
        console.log('📞 [CallFabric] Call joined event');
        await markCallActive();
      });

      call.on('call.updated', async (params: any) => {
        console.log('📞 [CallFabric] Call updated:', params);
        // If we get updated event, call is likely connected
        if (params?.state === 'active' || params?.node_id) {
          await markCallActive();
        }
      });

      call.on('destroy', async () => {
        console.log('📞 [CallFabric] Conference call destroyed');
        setCallState('idle');
        setActiveCall(null);
        activeCallRef.current = null;
        setIsInConference(false);
        isInConferenceRef.current = false;
        setConnectedCustomer(null);
        // Update backend if call had a DB ID
        if (dbCallIdForStatusUpdate) {
          try {
            await callsApi.updateStatus(dbCallIdForStatusUpdate, 'ended');
          } catch (err) {
            // Ignore
          }
        }
      });

      // Track the call
      setActiveCall(call);
      activeCallRef.current = call;
      conferenceCallRef.current = call;
      setCallState('ringing');

      // Set conference info for tracking
      const conferenceInfo: Conference = {
        id: 0,
        conferenceName: conferenceName,
        conferenceType: 'interaction',
        ownerUserId: user.id,
        status: 'active',
        createdAt: new Date().toISOString()
      };
      setAgentConference(conferenceInfo);
      agentConferenceRef.current = conferenceInfo;
      setIsInConference(true);
      isInConferenceRef.current = true;

      // Join the socket room for conference events
      if (socket) {
        const token = localStorage.getItem('access_token');
        socket.emit('join_conference', { conference_name: conferenceName, token });
      }

      // Start the call - this actually initiates the dial
      await call.start();

      // After call.start() completes, the call should be connected
      // Give it a moment then mark as active if not already
      setTimeout(async () => {
        if (!hasMarkedActive) {
          console.log('📞 [CallFabric] Fallback: marking call active after start() completed');
          await markCallActive();
        }
      }, 1000);

      // Set up connectedCustomer with AI context for UI display
      if (assignment.customerInfo) {
        const customer: ConnectedCustomer = {
          callId: assignment.callId || '',
          callDbId: assignment.callDbId,
          callerNumber: assignment.callerNumber || assignment.customerInfo.phone || '',
          queueId: assignment.queueId || '',
          conferenceName: conferenceName,
          customerInfo: {
            name: assignment.customerInfo.name,
            contact_id: assignment.customerInfo.contact_id || assignment.customerInfo.contactId,
          },
          aiContext: assignment.context || {},
          connectedAt: new Date()
        };
        setConnectedCustomer(customer);
        console.log('📋 [CallFabric] Set connectedCustomer with AI context:', assignment.context);

        // Also call the callback if set (for navigation/additional handling)
        if (onCustomerConnectedRef.current) {
          onCustomerConnectedRef.current(customer);
        }
      }

      console.log('✅ [CallFabric] Dialing out to conference...');

      // Clear any pending assignment
      setPendingCallAssignment(null);
    } catch (error) {
      console.error('❌ [CallFabric] Failed to accept call assignment:', error);
      throw error;
    }
  }, [user, client, socket]);

  // Reject a call assignment - customer remains in queue
  const rejectCallAssignment = useCallback(() => {
    if (!pendingCallAssignment) {
      return;
    }

    console.log('📞 [CallFabric] Rejecting call assignment:', pendingCallAssignment.conferenceName);

    // Notify backend that agent rejected the assignment
    if (socket) {
      const token = localStorage.getItem('access_token');
      socket.emit('reject_call_assignment', {
        call_id: pendingCallAssignment.callId,
        conference_name: pendingCallAssignment.conferenceName,
        token
      });
    }

    // Clear the pending assignment
    setPendingCallAssignment(null);
  }, [pendingCallAssignment, socket]);

  // Keep refs updated for use in setAgentStatus (avoids circular dependency)
  useEffect(() => {
    leaveAgentConferenceRef.current = leaveConference;
  }, [leaveConference]);

  // Keep refs updated for use in makeCall (avoids stale closures)
  useEffect(() => {
    isInConferenceRef.current = isInConference;
  }, [isInConference]);

  useEffect(() => {
    setAgentStatusRef.current = setAgentStatus;
  }, [setAgentStatus]);

  useEffect(() => {
    agentStatusRef.current = agentStatus;
  }, [agentStatus]);

  useEffect(() => {
    isClientReadyRef.current = isClientReady;
  }, [isClientReady]);

  useEffect(() => {
    pendingCallAssignmentRef.current = pendingCallAssignment;
  }, [pendingCallAssignment]);

  useEffect(() => {
    activeCallRef.current = activeCall;
  }, [activeCall]);

  useEffect(() => {
    agentConferenceRef.current = agentConference;
  }, [agentConference]);

  // Helper functions that update both state AND ref synchronously
  // This prevents race conditions where ref is stale when makeCall is called
  const setIsInConferenceSync = (value: boolean) => {
    isInConferenceRef.current = value;
    setIsInConference(value);
  };

  const setAgentConferenceSync = (value: Conference | null) => {
    agentConferenceRef.current = value;
    setAgentConference(value);
  };

  // Handle auto-restore when client becomes ready (after ICE gathering completes)
  // This handles page refresh: if agent was 'available', restore their online status
  // In per-interaction model, we just go online - no conference join until call assignment
  useEffect(() => {
    // Only run once when client becomes ready
    if (!client || !isClientReady || hasAttemptedAutoRejoinRef.current) {
      return;
    }

    // Check if we should auto-restore: persisted 'available' status
    const shouldRestore = pendingAutoRejoinRef.current || agentStatus === 'available';

    if (!shouldRestore) {
      console.log('📦 [CallFabric] Client ready, no auto-restore needed');
      console.log('  - agentStatus:', agentStatus);
      return;
    }

    console.log('🔄 [CallFabric] Client ready, auto-restoring available status...');
    console.log('  - pendingAutoRejoinRef:', pendingAutoRejoinRef.current);
    console.log('  - agentStatus:', agentStatus);
    hasAttemptedAutoRejoinRef.current = true;
    pendingAutoRejoinRef.current = false;

    // Perform the auto-restore (just go online, no conference join)
    const doRestore = async () => {
      setIsChangingStatus(true);
      setConferenceJoinError(null);

      try {
        if (!isOnline) {
          await goOnline();
        }
        // Update Redis status to available
        updateRedisStatus('available');
        console.log('✅ [CallFabric] Auto-restore successful - now available for call assignments');
        setConferenceJoinError(null);
      } catch (error: any) {
        console.error('❌ [CallFabric] Auto-restore failed:', error);
        setError('Failed to go online');
        setConferenceJoinError('Failed to connect - please try going available again');
        // Revert status since we couldn't go online
        setAgentStatusState('offline');
        sessionStorage.setItem('agent_status', 'offline');
        updateRedisStatus('offline');
      } finally {
        setIsChangingStatus(false);
      }
    };

    doRestore();
  }, [client, isClientReady]); // Minimal dependencies - only trigger on client ready

  // Listen for socket status updates
  useEffect(() => {
    if (!socket) return;

    const handleAgentStatus = (data: { status: AgentStatusType }) => {
      console.log('📥 [CallFabric] Status from server:', data);
      // Only update if not currently changing
      if (!isChangingStatus) {
        setAgentStatusState(data.status);
        // Note: Auto-rejoin is handled by the main useEffect that watches isClientReady
        // Just update state here - the useEffect will trigger rejoin if needed
      }
    };

    const handleAgentStatusUpdated = (data: { status: AgentStatusType }) => {
      console.log('✅ [CallFabric] Status confirmed:', data);
      setAgentStatusState(data.status);
      setIsChangingStatus(false);
    };

    socket.on('agent_status', handleAgentStatus);
    socket.on('agent_status_updated', handleAgentStatusUpdated);

    // On socket connect, fetch persisted status from server
    // If agent was 'available', auto-rejoin their conference
    if (connectionStatus === 'connected') {
      const token = localStorage.getItem('access_token');
      if (token) {
        console.log('🔄 [CallFabric] Socket connected, fetching persisted status...');
        socket.emit('get_agent_status', { token });
      }
    }

    return () => {
      socket.off('agent_status', handleAgentStatus);
      socket.off('agent_status_updated', handleAgentStatusUpdated);
    };
  }, [socket, connectionStatus, isChangingStatus]);

  // Conference socket event handlers
  useEffect(() => {
    if (!socket) return;

    // Handle participant joined event
    const handleParticipantJoined = (data: {
      conference_name: string;
      participant: ConferenceParticipant;
    }) => {
      console.log('📥 [CallFabric] Participant joined:', data);

      // Use ref to avoid stale closure
      if (agentConferenceRef.current?.conferenceName === data.conference_name) {
        setConferenceParticipants(prev => {
          // Avoid duplicates
          const exists = prev.some(p => p.participantId === data.participant.participantId);
          if (exists) {
            return prev.map(p =>
              p.participantId === data.participant.participantId ? data.participant : p
            );
          }
          return [...prev, data.participant];
        });
      }
    };

    // Handle participant left event
    const handleParticipantLeft = (data: {
      conference_name: string;
      participant_id: string;
    }) => {
      console.log('📥 [CallFabric] Participant left:', data);

      // Use ref to avoid stale closure
      if (agentConferenceRef.current?.conferenceName === data.conference_name) {
        setConferenceParticipants(prev =>
          prev.map(p =>
            p.participantId === data.participant_id
              ? { ...p, status: 'left' as const }
              : p
          )
        );
      }
    };

    // Handle customer routed to conference
    const handleCustomerRouted = (data: {
      call_id: string;
      call_db_id?: number;
      caller_number: string;
      queue_id: string;
      context: AICollectedContext;
      agent_id: number;
      agent_name: string;
      conference_name: string;
      customer_info: {
        phone: string;
        name?: string;
        contact_id?: number;
      };
    }) => {
      console.log('📥 [CallFabric] Customer routed to conference:', data);
      console.log('📥 [CallFabric] Current agent conference (ref):', agentConferenceRef.current?.conferenceName);

      // Use ref to get current value (avoid stale closure)
      const currentConference = agentConferenceRef.current;

      if (currentConference?.conferenceName === data.conference_name) {
        // Add the customer as a new participant
        const customerParticipant: ConferenceParticipant = {
          id: Date.now(), // Temporary ID
          conferenceId: currentConference.id,
          callId: data.call_db_id,
          participantType: 'customer',
          participantId: data.customer_info.phone,
          status: 'joining',
          joinedAt: new Date().toISOString(),
          isMuted: false,
          isDeaf: false
        };

        setConferenceParticipants(prev => [...prev, customerParticipant]);

        // Extract AI context from the data
        const aiContext: AICollectedContext = {
          ...data.context,
          // Also check global_data for additional fields
          ...(data.context?.global_data || {})
        };

        // Create the connected customer object
        const customer: ConnectedCustomer = {
          callId: data.call_id,
          callDbId: data.call_db_id,
          callerNumber: data.caller_number,
          queueId: data.queue_id,
          conferenceName: data.conference_name,
          customerInfo: data.customer_info,
          aiContext,
          connectedAt: new Date()
        };

        // Store the connected customer
        setConnectedCustomer(customer);

        // Call the callback if set (for navigation)
        if (onCustomerConnectedRef.current) {
          onCustomerConnectedRef.current(customer);
        }

        console.log('🔔 Customer connected:', data.customer_info.name || data.customer_info.phone);
        console.log('📋 AI Context:', aiContext);
      } else {
        console.log('⚠️ [CallFabric] Conference name mismatch or no conference. Expected:', currentConference?.conferenceName, 'Got:', data.conference_name);
      }
    };

    // Handle conference status update
    const handleConferenceUpdate = (data: {
      conference_id: number;
      conference_name: string;
      status: 'active' | 'ended';
      participant_count: number;
    }) => {
      console.log('📥 [CallFabric] Conference update:', data);

      // Use ref to avoid stale closure
      const currentConference = agentConferenceRef.current;
      if (currentConference?.conferenceName === data.conference_name) {
        const updated = {
          ...currentConference,
          id: data.conference_id,
          status: data.status
        };
        agentConferenceRef.current = updated;
        setAgentConference(updated);
      }
    };

    // Handle call assignment (NEW: per-interaction conference model)
    // When a customer is routed to this agent, we receive a call_assignment event
    // with the dial address to join the interaction conference
    const handleCallAssignment = (data: {
      call_id: string;
      call_db_id: number;
      caller_number: string;
      queue_id: string;
      context: any;
      agent_id: number;
      agent_name: string;
      conference_name: string;
      agent_call_sid?: string;  // Server-initiated call to agent
      customer_info: {
        phone: string;
        name?: string;
        contact_id?: number;
      };
    }) => {
      console.log('📥 [CallFabric] Call assignment received:', data);
      console.log('📞 Conference:', data.conference_name);
      console.log('👤 Customer:', data.customer_info);
      console.log('📱 Agent call SID:', data.agent_call_sid);

      // Set the pending call assignment
      // With server-initiated calls, the backend already called the agent
      // The agent will see an incoming call and can answer it
      const assignment: CallAssignment = {
        callId: data.call_id,
        callDbId: data.call_db_id,
        callerNumber: data.caller_number,
        queueId: data.queue_id,
        context: data.context,
        agentId: data.agent_id,
        agentName: data.agent_name,
        conferenceName: data.conference_name,
        agentCallSid: data.agent_call_sid,
        customerInfo: data.customer_info
      };

      setPendingCallAssignment(assignment);

      // The incoming call from the server will trigger the 'ringing' state
      // UI will show call info from this assignment + standard answer/reject buttons
      console.log('🔔 [CallFabric] Call assignment received - incoming call from server');
    };

    socket.on('conference_participant_joined', handleParticipantJoined);
    socket.on('conference_participant_left', handleParticipantLeft);
    socket.on('customer_routed_to_conference', handleCustomerRouted);
    socket.on('conference_update', handleConferenceUpdate);
    socket.on('call_assignment', handleCallAssignment);

    return () => {
      socket.off('conference_participant_joined', handleParticipantJoined);
      socket.off('conference_participant_left', handleParticipantLeft);
      socket.off('customer_routed_to_conference', handleCustomerRouted);
      socket.off('conference_update', handleConferenceUpdate);
      socket.off('call_assignment', handleCallAssignment);
    };
  // Note: We use refs (agentConferenceRef) inside handlers to avoid stale closures,
  // so we only need socket in dependencies
  }, [socket]);

  // Execute pending status change when client becomes ready
  useEffect(() => {
    if (isClientReady && pendingStatusRef.current) {
      const pendingStatus = pendingStatusRef.current;
      console.log('📱 [CallFabric] Client ready, executing pending status:', pendingStatus);
      pendingStatusRef.current = null;

      // Execute the pending status change - use setAgentStatus to get full behavior
      // including conference join
      setAgentStatus(pendingStatus);
    }
  }, [isClientReady, setAgentStatus]);

  // Note: We intentionally do NOT auto-restore 'available' status on page load
  // Agent must explicitly go available when ready to take calls

  // Initialize client on mount
  useEffect(() => {
    if (user && !client && !initializingRef.current) {
      initializeClient();
    }

    return () => {
      if (rootElementRef.current) {
        rootElementRef.current.remove();
      }
      // Clean up ICE ready timer
      if (iceReadyTimerRef.current) {
        clearTimeout(iceReadyTimerRef.current);
      }
    };
  }, [user]);

  // Helper functions for connected customer
  const setOnCustomerConnected = useCallback((callback: ((customer: ConnectedCustomer) => void) | undefined) => {
    onCustomerConnectedRef.current = callback;
  }, []);

  const clearConnectedCustomer = useCallback(() => {
    setConnectedCustomer(null);
  }, []);

  const value: CallFabricContextType = {
    client,
    activeCall,
    isOnline,
    isInitializing,
    isClientReady,
    error,
    callState,
    isMuted,
    micPermission,
    agentStatus,
    isChangingStatus,
    conferenceJoinError,
    // Conference state
    agentConference,
    conferenceParticipants,
    isInConference,
    // Connected customer state
    connectedCustomer,
    onCustomerConnected: onCustomerConnectedRef.current,
    setOnCustomerConnected,
    clearConnectedCustomer,
    // Actions
    setAgentStatus,
    initializeClient,
    makeCall,
    makeCallToSwml,
    hangup,
    answerCall,
    requestMicPermission,
    mute: async () => { await activeCall?.mute(); setIsMuted(true); },
    unmute: async () => { await activeCall?.unmute(); setIsMuted(false); },
    hold: async () => { await activeCall?.hold(); },
    unhold: async () => { await activeCall?.unhold(); },
    sendDigits: async (digits: string) => { await activeCall?.sendDigits(digits); },
    // Conference actions (per-interaction model)
    joinInteractionConference,
    leaveConference,
    // Pending call assignment
    pendingCallAssignment,
    acceptCallAssignment,
    acceptCallAssignmentWithData,
    rejectCallAssignment
  };

  return (
    <CallFabricContext.Provider value={value}>
      {children}
    </CallFabricContext.Provider>
  );
}
