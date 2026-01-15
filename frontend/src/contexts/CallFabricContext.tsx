import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useSocketContext } from './SocketContext';
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

  // Conference actions
  joinAgentConference: () => Promise<void>;
  leaveAgentConference: () => Promise<void>;
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

  // Refs for conference functions to avoid circular dependency in setAgentStatus
  const joinAgentConferenceRef = useRef<() => Promise<void>>(() => Promise.resolve());
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

  // Load SignalWire SDK - using @dev for latest Call Fabric features
  // Add cache buster to ensure we get the latest version
  const loadSignalWireSDK = () => {
    return new Promise((resolve, reject) => {
      if (window.SignalWire) {
        console.log('✅ SignalWire SDK already loaded');
        resolve(true);
        return;
      }
      const script = document.createElement('script');
      // Use date-based cache buster (changes daily) to get fresh SDK
      const cacheBuster = new Date().toISOString().split('T')[0];
      script.src = `https://unpkg.com/@signalwire/client@dev?v=${cacheBuster}`;
      script.async = true;
      script.onload = () => {
        console.log('✅ SignalWire SDK loaded from unpkg');
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
        rootElementRef.current = document.createElement('div');
        rootElementRef.current.id = 'signalwire-root';
        rootElementRef.current.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;';
        document.body.appendChild(rootElementRef.current);
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
            console.log('📞 [CallFabric] Incoming call:', notification);
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
                const call = await notification.invite.accept({
                  rootElement: rootElementRef.current
                });
                setCallState('active');
                return call;
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

            const autoAnswer = localStorage.getItem('auto_answer') === 'true';
            if (autoAnswer) {
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
      if (newStatus === 'available') {
        if (!isOnline) {
          await goOnline();
        }
        // Join agent conference for hot-seat mode with retry logic
        // ICE gathering can take ~10.5 seconds, so we retry if it fails
        if (!isInConference) {
          let conferenceJoined = false;
          const maxRetries = 5;
          const baseDelay = 2000;

          for (let attempt = 1; attempt <= maxRetries && !conferenceJoined; attempt++) {
            try {
              setConferenceJoinError(attempt > 1 ? `Connecting... (attempt ${attempt}/${maxRetries})` : null);
              await joinAgentConferenceRef.current();
              conferenceJoined = true;
              setConferenceJoinError(null);
            } catch (confError: any) {
              console.error(`⚠️ [CallFabric] Conference join attempt ${attempt} failed:`, confError);

              if (attempt < maxRetries) {
                const delay = baseDelay * attempt;
                console.log(`⏳ [CallFabric] Retrying conference join in ${delay / 1000}s...`);
                await new Promise(resolve => setTimeout(resolve, delay));
              } else {
                // All retries exhausted - this is now a real failure
                setConferenceJoinError('Failed to join conference - calls may not be routed to you');
                console.error('❌ [CallFabric] Conference join failed after all retries');
              }
            }
          }
        }
      } else if (newStatus === 'offline' || newStatus === 'break') {
        if (isOnline) {
          await goOffline();
        }
        // Leave agent conference (use ref to avoid circular dep)
        if (isInConference) {
          try {
            await leaveAgentConferenceRef.current();
          } catch (confError) {
            console.error('⚠️ [CallFabric] Failed to leave conference:', confError);
          }
        }
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
  const answerCall = useCallback(async () => {
    if (!activeCall || activeCall.direction !== 'inbound') return;
    try {
      await activeCall.answer();
      setCallState('active');
    } catch (error) {
      console.error('Failed to answer call:', error);
    }
  }, [activeCall]);

  // Join agent's personal conference (for "hot seat" mode)
  // Requires a CXML Script resource configured in SignalWire Dashboard:
  // 1. Go to Resources > Add New > Script > CXML Script
  // 2. Set Request URL to: https://your-ngrok.io/api/conferences/agent-conference
  // 3. Note the assigned address (e.g., /public/agent-conference)
  // 4. Set AGENT_CONFERENCE_RESOURCE env var to that address
  const joinAgentConference = useCallback(async () => {
    if (!client || !user) {
      console.log('⚠️ [CallFabric] Cannot join conference - client or user not ready');
      return;
    }

    if (isInConference) {
      console.log('⚠️ [CallFabric] Already in conference');
      return;
    }

    try {
      console.log('📞 [CallFabric] Joining agent conference...');

      // Get the resource address from the backend
      const response = await fetch(`/api/conferences/agent/${user.id}/resource-address`, {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (!response.ok) {
        throw new Error('Failed to get conference resource address');
      }

      const { dial_address, conference_name } = await response.json();
      console.log('📞 [CallFabric] Dialing resource:', dial_address);

      // Dial the resource address (e.g., /public/agent-conference?agent_id=4)
      const call = await client.dial({
        to: dial_address,
        rootElement: rootElementRef.current,
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user.id,
          call_type: 'conference',
          conference_name
        }
      });

      // Set conference info BEFORE starting the call so ref is ready
      const conferenceInfo: Conference = {
        id: 0, // Will be updated by status callback
        conferenceName: conference_name,
        conferenceType: 'agent',
        ownerUserId: user.id,
        status: 'active',
        createdAt: new Date().toISOString()
      };
      setAgentConferenceSync(conferenceInfo);

      call.on('call.state', (state: any) => {
        console.log('📞 [CallFabric] Conference call state:', state);
        if (state === 'active' || state === 'answered') {
          setIsInConferenceSync(true);
          setConferenceJoinError(null);
        } else if (state === 'ending' || state === 'ended') {
          setIsInConferenceSync(false);
          conferenceCallRef.current = null;
        }
      });

      call.on('destroy', () => {
        console.log('📞 [CallFabric] Conference call destroyed');
        setIsInConferenceSync(false);
        conferenceCallRef.current = null;
      });

      conferenceCallRef.current = call;
      await call.start();

      // Join socket room for conference updates
      if (socket) {
        const token = localStorage.getItem('access_token');
        socket.emit('join_conference', { conference_name, token });
      }

      console.log('✅ [CallFabric] Joined conference:', conference_name);

    } catch (error) {
      console.error('❌ [CallFabric] Failed to join conference:', error);
      setError('Failed to join conference');
    }
  }, [client, user, isInConference, socket]);

  // Leave agent's conference
  const leaveAgentConference = useCallback(async () => {
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

  // Keep refs updated for use in setAgentStatus (avoids circular dependency)
  useEffect(() => {
    joinAgentConferenceRef.current = joinAgentConference;
  }, [joinAgentConference]);

  useEffect(() => {
    leaveAgentConferenceRef.current = leaveAgentConference;
  }, [leaveAgentConference]);

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

  // Handle auto-rejoin when client becomes ready (after ICE gathering completes)
  // This handles page refresh: if agent was 'available', auto-rejoin their conference
  // The 11s ICE delay means client should be truly ready when isClientReady becomes true
  useEffect(() => {
    // Only run once when client becomes ready
    if (!client || !isClientReady || hasAttemptedAutoRejoinRef.current) {
      return;
    }

    // Check if we should auto-rejoin: persisted 'available' status and not already in conference
    const shouldRejoin = (pendingAutoRejoinRef.current || agentStatus === 'available') && !isInConference;

    if (!shouldRejoin) {
      console.log('📦 [CallFabric] Client ready, no auto-rejoin needed');
      console.log('  - agentStatus:', agentStatus, 'isInConference:', isInConference);
      return;
    }

    console.log('🔄 [CallFabric] Client ready, auto-rejoining conference...');
    console.log('  - pendingAutoRejoinRef:', pendingAutoRejoinRef.current);
    console.log('  - agentStatus:', agentStatus);
    hasAttemptedAutoRejoinRef.current = true;
    pendingAutoRejoinRef.current = false;

    // Perform the auto-rejoin
    const doRejoin = async () => {
      setIsChangingStatus(true);
      setConferenceJoinError(null);

      try {
        if (!isOnline) {
          await goOnline();
        }
        await joinAgentConferenceRef.current();
        console.log('✅ [CallFabric] Auto-rejoin successful');
        setConferenceJoinError(null);
      } catch (error: any) {
        console.error('❌ [CallFabric] Auto-rejoin failed:', error);
        setError('Failed to rejoin conference');
        setConferenceJoinError('Failed to connect - please try going available again');
        // Revert status since we couldn't rejoin
        setAgentStatusState('offline');
        sessionStorage.setItem('agent_status', 'offline');
        updateRedisStatus('offline');
      } finally {
        setIsChangingStatus(false);
      }
    };

    doRejoin();
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

    socket.on('conference_participant_joined', handleParticipantJoined);
    socket.on('conference_participant_left', handleParticipantLeft);
    socket.on('customer_routed_to_conference', handleCustomerRouted);
    socket.on('conference_update', handleConferenceUpdate);

    return () => {
      socket.off('conference_participant_joined', handleParticipantJoined);
      socket.off('conference_participant_left', handleParticipantLeft);
      socket.off('customer_routed_to_conference', handleCustomerRouted);
      socket.off('conference_update', handleConferenceUpdate);
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
    // Conference actions
    joinAgentConference,
    leaveAgentConference
  };

  return (
    <CallFabricContext.Provider value={value}>
      {children}
    </CallFabricContext.Provider>
  );
}
