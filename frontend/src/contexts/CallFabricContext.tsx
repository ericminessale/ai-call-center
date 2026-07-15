import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useVerifyStore } from '../stores/verifyStore';
import { useSocketContext } from './SocketContext';
import { conferencesApi, callsApi } from '../services/api';
import type { Conference, ConferenceParticipant } from '../types/callcenter';
import { logger } from '../lib/logger';

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
  isClientReady: boolean; // True when client is initialized and usable
  error: string | null;
  // Set when client.online() fails with code -32603 ("WebRTC endpoint
  // registration failed"). Distinct from generic `error` because it has a
  // specific recovery path (clear cached SDK state + reload). Header
  // surfaces this with a dedicated "Reset" button. See `resetCallFabricState`.
  registrationError: string | null;
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

  // Recovery: nuke cached Call Fabric state in the browser and reload.
  // Targets the recurring "WebRTC endpoint registration failed" SDK error
  // that surfaces when a previous session left a stale subscriber binding
  // in the SDK's localStorage (`sw:*` keys) or when push registration
  // conflicts with the new online() call. Pretty much always fixes it.
  resetCallFabricState: () => Promise<void>;

  // Takeover calls (connect to existing call via SWML)
  makeCallToSwml: (swmlUrl: string, context?: any) => Promise<any>;

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
  // Multi-agent conference fields
  assignmentType?: 'normal' | 'backup' | 'escalation';
  requestingAgent?: { id: number; name: string; email: string };
  whisperMode?: boolean;
  targetAgentCallSid?: string;  // For coach/whisper mode — the agent's call SID to coach
  legId?: number;
  // Call transport (M1+). Conference mode dial-into-conf flow is the legacy
  // default; bridge mode means the agent's SDK already has a native invite
  // ringing (from queue-pickup SWML), so the accept handler must `invite.accept()`
  // instead of joining a conference. See CALL_TRANSPORT.md.
  transport?: 'conference' | 'bridge';
}

const CallFabricContext = createContext<CallFabricContextType | null>(null);

export function useCallFabricContext() {
  const context = useContext(CallFabricContext);
  if (!context) {
    throw new Error('useCallFabricContext must be used within a CallFabricProvider');
  }
  return context;
}

/**
 * Mute / unmute the local audio track for a SignalWire Call Fabric call.
 *
 * Why this exists: ``call.audioMute()`` throws
 * ``CapabilityError: Missing audio mute capability`` on calls that landed
 * via ``client.dial()`` into a conference SWML resource — the per-call
 * capability set the SDK enforces internally doesn't include audio_mute for
 * that path on @signalwire/client@dev. The conference verb's permissions
 * don't grant it either, and we have no client-side knob to flip.
 *
 * Strategy: try every SDK shape we know about (audioMute/audioUnmute,
 * setAudioMuted, mute/unmute), and if all of those refuse, walk the
 * RTCPeerConnection ourselves and zero the outgoing audio track's
 * ``enabled`` flag. That's a standard WebRTC mute — the browser stops
 * sending audio frames upstream, so SignalWire's conference mixer hears
 * silence regardless of what the SDK's capability matrix thinks.
 *
 * The peer object hangs off the SDK call under a handful of names
 * depending on SDK version, so we probe a few candidates.
 */
async function setLocalAudioMuted(
  activeCall: any,
  muted: boolean,
  setIsMuted: (v: boolean) => void,
): Promise<void> {
  if (!activeCall) {
    logger.warn('[Mute] No active call');
    return;
  }

  // Try every documented SDK method, in order from most-specific to most-
  // generic. Wrap each in try/catch — a CapabilityError on one method
  // doesn't mean another won't work.
  const sdkAttempts: Array<[string, (() => Promise<void>) | undefined]> = muted
    ? [
        ['setAudioMuted(true)',  activeCall.setAudioMuted ? () => activeCall.setAudioMuted(true)  : undefined],
        ['audioMute()',          activeCall.audioMute    ? () => activeCall.audioMute()           : undefined],
        ['mute()',               activeCall.mute         ? () => activeCall.mute()                : undefined],
      ]
    : [
        ['setAudioMuted(false)', activeCall.setAudioMuted ? () => activeCall.setAudioMuted(false) : undefined],
        ['audioUnmute()',        activeCall.audioUnmute  ? () => activeCall.audioUnmute()         : undefined],
        ['unmute()',             activeCall.unmute       ? () => activeCall.unmute()              : undefined],
      ];

  for (const [name, fn] of sdkAttempts) {
    if (!fn) continue;
    try {
      await fn();
      logger.debug(`[Mute] SDK ${name} succeeded`);
      setIsMuted(muted);
      return;
    } catch (err: any) {
      // CapabilityError or any other rejection — keep trying.
      logger.warn(`[Mute] SDK ${name} threw, trying next: ${err?.message || err}`);
    }
  }

  // Direct-track fallback. Pull the RTCPeerConnection off whichever
  // property name the current SDK build is using. Property names observed
  // across @signalwire/client@dev builds: ``peer``, ``peer.instance``,
  // ``rtcPeerConnection``, plus a getter ``getRTCPeerConnection()``.
  const peer: RTCPeerConnection | undefined =
    activeCall.peer?.instance
    || activeCall.peer
    || activeCall.rtcPeerConnection
    || (typeof activeCall.getRTCPeerConnection === 'function'
        ? activeCall.getRTCPeerConnection()
        : undefined);

  if (!peer || typeof peer.getSenders !== 'function') {
    logger.error('[Mute] No SDK method worked and no RTCPeerConnection found — cannot mute');
    return;
  }

  let toggled = 0;
  for (const sender of peer.getSenders()) {
    if (sender.track?.kind === 'audio') {
      sender.track.enabled = !muted; // enabled=false ⇒ muted
      toggled++;
    }
  }
  if (toggled === 0) {
    logger.error('[Mute] RTCPeerConnection had no audio senders to toggle');
    return;
  }
  logger.debug(`[Mute] Fallback: toggled track.enabled on ${toggled} audio sender(s) → muted=${muted}`);
  setIsMuted(muted);
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
  const [registrationError, setRegistrationError] = useState<string | null>(null);
  const [callState, setCallState] = useState<'idle' | 'ringing' | 'active' | 'ending'>('idle');
  const [isMuted, setIsMuted] = useState(false);
  const [micPermission, setMicPermission] = useState<'granted' | 'denied' | 'prompt' | 'unknown'>('unknown');

  // Agent status state - check sessionStorage for persisted status
  // sessionStorage persists across page refreshes but clears when tab/browser closes
  // This means: new session = offline, page refresh = restore previous status
  const getInitialStatus = (): AgentStatusType => {
    const persisted = sessionStorage.getItem('agent_status');
    if (persisted === 'available') {
      logger.debug('📦 [CallFabric] Found persisted status in sessionStorage: available');
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

  // Ref for client to avoid stale closures in makeCall
  const clientRef = useRef<CallFabricClient | null>(null);

  // Ref for activeCall to avoid stale closures in answerCall
  const activeCallRef = useRef<ActiveCall | null>(null);

  // Ref to track the outbound dialed call's DB ID (for cleanup when SDK fires destroy)
  const outboundDialCallIdRef = useRef<number | null>(null);

  // Ref for leaveConference to avoid circular dependency in setAgentStatus
  const leaveAgentConferenceRef = useRef<() => Promise<void>>(() => Promise.resolve());

  // Refs for state values that need to be accessed in makeCall without stale closures
  const isInConferenceRef = useRef<boolean>(false);
  const setAgentStatusRef = useRef<(status: AgentStatusType) => Promise<void>>(() => Promise.resolve());
  const agentStatusRef = useRef<AgentStatusType>(getInitialStatus()); // Match initial state
  const agentConferenceRef = useRef<Conference | null>(null);

  // Client readiness - set to true once client is initialized
  const [isClientReady, setIsClientReady] = useState(false);
  const hasAttemptedAutoRejoinRef = useRef<boolean>(false);  // Ensure auto-rejoin only runs once per session

  const { user } = useAuthStore();
  const { socket, connectionStatus } = useSocketContext();
  const rootElementRef = useRef<HTMLDivElement | null>(null);
  const inviteRef = useRef<any>(null);
  const takeoverCallSidRef = useRef<string | null>(null);
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
      logger.error('Error getting subscriber token:', error);
      throw error;
    }
  };

  // Load WebRTC adapter for cross-browser compatibility (like SDK example does)
  const loadWebRTCAdapter = () => {
    return new Promise<void>((resolve, reject) => {
      // Check if adapter is already loaded
      if ((window as any).adapter) {
        logger.debug('✅ WebRTC adapter already loaded');
        resolve();
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://webrtc.github.io/adapter/adapter-latest.js';
      script.async = true;
      script.onload = () => {
        logger.debug('✅ WebRTC adapter loaded');
        resolve();
      };
      script.onerror = (err) => {
        logger.warn('⚠️ Failed to load WebRTC adapter, continuing anyway:', err);
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
        logger.debug('✅ SignalWire SDK already loaded');
        resolve(true);
        return;
      }
      const script = document.createElement('script');
      // Pin to the @dev channel (1.0.0-dev.* prereleases) rather than the
      // npm `latest` tag. `latest` currently points at 0.0.1 — a placeholder
      // release predating the actual Call Fabric subscriber surface; real
      // active development (where the `verto.answer` accept fix lives) is
      // on the @dev track. Git history (commit 06d5073) confirms the
      // pre-conference working setup also used @dev — switching to
      // `latest` happened AFTER the conference migration when inbound
      // accept was no longer load-bearing for us.
      //
      // Pin to the @dev channel (1.0.0-dev.* prereleases). The npm `latest`
      // tag points at 0.0.1 — a placeholder release predating the actual
      // Call Fabric subscriber surface; real active development lives on
      // @dev. Cache-buster (yyyy-mm-dd) keeps daily builds fresh without
      // the user having to clear their browser cache.
      const cacheBuster = new Date().toISOString().split('T')[0];
      script.src = `https://unpkg.com/@signalwire/client@dev?v=${cacheBuster}`;
      script.async = true;
      script.onload = () => {
        logger.debug('✅ SignalWire SDK loaded (@signalwire/client@dev)');
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
      logger.debug('📱 [CallFabric] Initializing client...');

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
        logger.debug('📞 [CallFabric] Created rootElement for media (matching SDK example)');
      }

      // Match reference implementation pattern for host parameter:
      // explicitly pass undefined when not set, rather than empty string
      const swHost = import.meta.env.VITE_SIGNALWIRE_HOST;
      const hostParam = swHost && swHost.trim().length ? swHost : undefined;
      logger.debug('📱 [CallFabric] Using host:', hostParam || '(default from token)');

      // Gate verbose SDK logging behind an env var so dev can opt in without
      // shipping WebSocket frame traces to every production browser session.
      const swDebug = import.meta.env.VITE_SW_DEBUG === 'true';
      const swClient = await SWire({
        token: token,
        host: hostParam,
        ...(swDebug ? { debug: { logWsTraffic: true }, logLevel: 'debug' as const } : {}),
      });

      setClient(swClient);
      setIsClientReady(true);
      logger.debug('✅ [CallFabric] Client initialized and ready');

    } catch (error) {
      logger.error('❌ [CallFabric] Failed to initialize:', error);
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
      logger.debug('📱 [CallFabric] Going online...');

      await client.online({
        incomingCallHandlers: {
          all: async (notification: any) => {
            logger.debug('📞 [CallFabric] Incoming call notification:', notification);
            logger.debug('📞 [CallFabric] Incoming invite object:', notification.invite);
            logger.debug('📞 [CallFabric] Incoming invite details:', JSON.stringify(notification.invite?.details, null, 2));
            logger.debug('📞 [CallFabric] Incoming callID:', notification.invite?.details?.callID);
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
                logger.debug('📞 [CallFabric] answer() called');
                logger.debug('📞 [CallFabric] notification.invite:', notification.invite);
                logger.debug('📞 [CallFabric] notification.invite.details:', notification.invite?.details);
                logger.debug('📞 [CallFabric] callID:', notification.invite?.details?.callID);
                logger.debug('📞 [CallFabric] from:', notification.invite?.details?.from);
                logger.debug('📞 [CallFabric] rootElement:', rootElementRef.current);

                try {
                  // Call accept() with audio-only (no video) since this is a voice call center
                  logger.debug('📞 [CallFabric] Calling notification.invite.accept() with audio-only...');
                  const call = await notification.invite.accept({
                    rootElement: rootElementRef.current,
                    audio: true,
                    video: false,  // Voice-only call - don't request camera
                  });

                  logger.debug('📞 [CallFabric] accept() returned:', call);
                  logger.debug('📞 [CallFabric] call.id:', call?.id);
                  logger.debug('📞 [CallFabric] call.state:', call?.state);
                  logger.debug('📞 [CallFabric] MISMATCH CHECK - invite callID:', notification.invite?.details?.callID, 'vs call.id:', call?.id);

                  // Set up comprehensive event handlers to debug
                  if (call) {
                    // Store globally for debugging
                    (window as any).__swCall = call;
                    (window as any).__swInvite = notification.invite;

                    call.on('destroy', () => {
                      logger.warn('📞 [CallFabric] Inbound call destroyed');
                      setCallState('idle');
                      setActiveCall(null);
                      activeCallRef.current = null;
                    });

                    // Add more event listeners to debug
                    call.on('call.state', (state: any) => {
                      logger.debug('📞 [CallFabric] call.state event:', state);
                    });

                    call.on('room.joined', (params: any) => {
                      logger.debug('📞 [CallFabric] room.joined event:', params);
                    });

                    call.on('media.connected', () => {
                      logger.debug('📞 [CallFabric] media.connected event');
                    });

                    call.on('media.disconnected', () => {
                      logger.debug('📞 [CallFabric] media.disconnected event');
                    });
                  }

                  setCallState('active');
                  return call;
                } catch (acceptError) {
                  logger.error('❌ [CallFabric] accept() threw error:', acceptError);
                  logger.error('❌ [CallFabric] Error details:', JSON.stringify(acceptError, null, 2));
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
            logger.debug('📞 [CallFabric] ActiveCall set:', incomingCall.id);

            const autoAnswer = localStorage.getItem('auto_answer') === 'true';
            if (autoAnswer) {
              logger.debug('📞 [CallFabric] Auto-answer enabled, answering in 1s...');
              setTimeout(() => incomingCall.answer(), 1000);
            }
          }
        }
      });

      setIsOnline(true);
      setRegistrationError(null);  // success path — any prior reg error is stale
      logger.debug('✅ [CallFabric] Now online and ready to receive calls');

    } catch (err: any) {
      logger.error('❌ [CallFabric] Failed to go online:', err);
      // SignalWire code -32603 (WebRTC endpoint registration failed) means
      // the SDK couldn't register the device — usually because a previous
      // session left a stale subscriber binding. Surface a dedicated error
      // state so the header can show a "Reset" button targeting this exact
      // class of failure. Other errors fall through to the generic banner.
      const code = err?.code ?? err?.original_error?.code;
      const msg = err?.message ?? err?.original_error?.message ?? '';
      if (code === -32603 || /registration failed/i.test(msg)) {
        setRegistrationError(
          'Call Fabric registration failed. Click Reset to clear cached SDK state and reload.'
        );
      } else {
        setError('Failed to go online');
      }
      throw err;
    }
  }, [client, isOnline]);

  // Go offline in Call Fabric
  const goOffline = useCallback(async () => {
    if (!client || !isOnline) return;

    try {
      logger.debug('📱 [CallFabric] Going offline...');
      await client.offline();
      setIsOnline(false);
      logger.debug('✅ [CallFabric] Now offline');
    } catch (error) {
      logger.error('❌ [CallFabric] Failed to go offline:', error);
      throw error;
    }
  }, [client, isOnline]);

  // Update Redis status via socket
  const updateRedisStatus = useCallback((status: AgentStatusType) => {
    if (!socket) {
      logger.debug('❌ [CallFabric] No socket for Redis update');
      return;
    }

    const token = localStorage.getItem('access_token');
    if (token) {
      logger.debug('📤 [CallFabric] Updating Redis status:', status);
      socket.emit('set_agent_status', { token, status });
    }
  }, [socket]);

  // UNIFIED: Set agent status (controls both Call Fabric and Redis)
  const setAgentStatus = useCallback(async (newStatus: AgentStatusType) => {
    logger.debug('🔄 [CallFabric] setAgentStatus called:', newStatus);
    logger.debug('  - client:', !!client, 'isOnline:', isOnline);
    logger.debug('  - socket:', !!socket, 'connected:', connectionStatus);

    // If client isn't initialized yet, handle gracefully
    if (!client) {
      if (newStatus === 'available') {
        logger.debug('⏳ [CallFabric] Client not initialized, cannot go available yet');
        setError('Phone system not initialized yet - please wait');
        return;
      } else {
        // For offline/break, just update Redis without Call Fabric
        logger.debug('📤 [CallFabric] Client not ready, updating Redis only for:', newStatus);
        updateRedisStatus(newStatus);
        setAgentStatusState(newStatus);
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
        logger.debug('✅ [CallFabric] Agent available - ready to receive call assignments');
      } else if (newStatus === 'offline' || newStatus === 'break') {
        if (isOnline) {
          await goOffline();
        }
        // Leave any active conference
        if (isInConference) {
          try {
            await leaveAgentConferenceRef.current();
          } catch (confError) {
            logger.error('⚠️ [CallFabric] Failed to leave conference:', confError);
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
      logger.debug('✅ [CallFabric] Status changed to:', newStatus);

    } catch (error: any) {
      logger.error('❌ [CallFabric] Failed to change status:', error);
      // Don't leak raw SDK errors into the UI. The technical detail is in
      // the log above; show the agent something friendly + actionable.
      // "client.online is not a function" / "undefined" class errors mean
      // the SDK script didn't load (or a stale/incompatible build is
      // cached) — tell them to refresh. Everything else gets a generic
      // retry message.
      const raw = error?.message || '';
      const friendlyMsg = /not a function|undefined|is not defined|cannot read/i.test(raw)
        ? 'Phone system is still loading — please refresh the page and try again.'
        : 'Could not change your status. Please try again.';
      setError(friendlyMsg);
      setConferenceJoinError(friendlyMsg);

      // If going available failed, revert to offline
      if (newStatus === 'available') {
        logger.debug('⚠️ [CallFabric] Reverting to offline due to error');
        setAgentStatusState('offline');
        sessionStorage.setItem('agent_status', 'offline');
        updateRedisStatus('offline');
      }
    } finally {
      setIsChangingStatus(false);
    }
  }, [client, isOnline, isInConference, socket, connectionStatus, goOnline, goOffline, updateRedisStatus]);

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
    // Hosted-demo outbound is VERIFY-FIRST: a workspace's telephony surface
    // is the visitor's own verified phone. Agent free-form WebRTC dial to an
    // arbitrary number isn't part of that model, so we refuse it at the
    // source (before backend/WebRTC setup) — but with an HONEST, verify-aware
    // message that points to what actually works, not the old blanket
    // "not available in demo mode." Backend endpoints
    // (/api/conferences/<n>/dial-out, /api/calls/initiate,
    // /api/ai/outbound-call) enforce the real own-number-only rule; this
    // gate covers the browser-direct SDK dial that bypasses the backend.
    const isDemo = useAuthStore.getState().runtimeConfig?.demo_mode;
    if (isDemo) {
      const { default: toast } = await import('react-hot-toast');
      const authUser = useAuthStore.getState().user;
      const isVisitor = authUser != null && authUser.workspace_id != null;
      if (!isVisitor) {
        // Platform operator — no workspace / verified number of their own.
        toast.error("Outbound calling isn't available on the operator account.");
        const err: any = new Error('operator outbound unavailable');
        err.code = 'demo_blocked';
        throw err;
      }
      if (!useVerifyStore.getState().verified) {
        toast.error('Verify your phone first — then the demo can place calls to your number.');
        const err: any = new Error('verify required before outbound');
        err.code = 'demo_verify_required';
        throw err;
      }
      // Verified: the demo places calls to YOUR verified number only. The
      // showcase outbound is "Have the AI call me" (AI dials your phone);
      // free-form dial to some other number is intentionally out of scope.
      toast.error('In the demo, outbound goes to your verified number — use "Have the AI call me."');
      const err: any = new Error('demo dials the verified number only');
      err.code = 'demo_blocked';
      throw err;
    }

    const token = localStorage.getItem('access_token');

    // Helper function to dial out via backend API (joins call to agent's conference)
    // NOTE: We do NOT set callState here because the call is server-initiated.
    // Call state updates will come via Socket.IO 'call_update' events which update
    // the activeCallForContact prop in the UI components.
    const dialOutToConference = async (conferenceName: string, contactId?: number) => {
      logger.debug('📞 [CallFabric] Dial-out via conference:', conferenceName);

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
      logger.debug('✅ [CallFabric] Dial-out initiated:', data);

      // Track the outbound call DB ID for cleanup (in case SDK destroy fires
      // before the backend's call_state_webhook delivers the 'ended' event)
      if (data.call_id) {
        outboundDialCallIdRef.current = data.call_id;
      }

      // The call is being placed by the server, not the browser
      // The agent is already in the conference and will hear the call when answered
      // Set callState to 'ringing' for immediate UI feedback
      // The call_update socket event will provide further updates
      setCallState('ringing');

      return data;
    };

    try {
      // Use refs for current state values (avoids stale closures)
      const currentIsInConference = isInConferenceRef.current;
      const currentAgentConference = agentConferenceRef.current;
      const currentAgentStatus = agentStatusRef.current;

      logger.debug('📞 [CallFabric] makeCall called:', {
        phoneNumber,
        currentIsInConference,
        currentAgentStatus,
        currentAgentConference: currentAgentConference?.conferenceName
      });

      // Case 1: Agent already in a conference - dial out directly
      if (currentAgentConference && currentIsInConference) {
        logger.debug('📞 [CallFabric] Case 1: Already in conference, using dial-out API');
        const result = await dialOutToConference(
          currentAgentConference.conferenceName,
          context?.contact_id
        );
        return result;
      }

      // Case 2: Agent is available (or offline) but not in a conference
      // Create an outbound conference on-the-fly, join it, then dial out
      if (currentAgentStatus === 'available' || currentAgentStatus === 'offline') {
        logger.debug(`📞 [CallFabric] Case 2: Agent ${currentAgentStatus}, creating outbound conference...`);
        setIsChangingStatus(true);

        try {
          // If offline, go available first
          if (currentAgentStatus === 'offline') {
            logger.debug('📞 [CallFabric] Going available...');
            await setAgentStatusRef.current('available');
          }

          // Create an outbound conference name
          const confName = `outbound-${user?.id}-${Date.now()}`;
          logger.debug('📞 [CallFabric] Creating outbound conference:', confName);

          // Prepare the conference join (stores params in Redis, returns dial address)
          const prepareResponse = await conferencesApi.prepareJoin({
            agent_id: user?.id,
            conference_name: confName
          });
          const dialAddress = prepareResponse.data.dial_address;
          logger.debug('📞 [CallFabric] Got dial address:', dialAddress);

          // Dial the conference resource (agent joins conference)
          const currentClient = clientRef.current;
          if (!currentClient) {
            throw new Error('Phone system not initialized');
          }
          const call = await currentClient.dial({
            to: dialAddress,
            rootElement: rootElementRef.current,
            audio: true,
            video: false,
            logLevel: 'debug',
            debug: { logWsTraffic: true },
            userVariables: {
              agent_id: user?.id,
              call_type: 'outbound_conference',
              conference_name: confName,
              token: prepareResponse.data.token
            }
          });

          // Set up call event handlers
          let outboundCallDbId: number | null = null; // Populated after dial-out, used in destroy handler
          let hasMarkedActive = false;
          const markActive = () => {
            if (hasMarkedActive) return;
            hasMarkedActive = true;
            logger.debug('✅ [CallFabric] Outbound conference ACTIVE');
            setCallState('active');
            setIsInConference(true);
            isInConferenceRef.current = true;
          };

          const connectedStates = ['active', 'answered', 'answering', 'early', 'trying'];

          call.on('call.state', (state: any) => {
            logger.debug('📞 [CallFabric] Outbound conf call state:', state);
            if (connectedStates.includes(state)) markActive();
            else if (state === 'ended' || state === 'hangup' || state === 'destroy') {
              setCallState('idle');
              setActiveCall(null);
              activeCallRef.current = null;
              setIsInConference(false);
              isInConferenceRef.current = false;
              setAgentConference(null);
              agentConferenceRef.current = null;
              setConnectedCustomer(null);

              // Ensure backend marks the outbound call as ended
              const callId = outboundCallDbId || outboundDialCallIdRef.current;
              if (callId) {
                logger.debug('📞 [CallFabric] Marking outbound call as ended in backend:', callId);
                callsApi.updateStatus(callId, 'ended').catch(() => {});
              }
            }
          });

          call.on('call.joined', () => markActive());
          call.on('call.updated', (params: any) => {
            if (params?.state === 'active' || params?.node_id) markActive();
          });

          call.on('destroy', () => {
            logger.debug('📞 [CallFabric] Outbound conference call destroyed');
            setCallState('idle');
            setActiveCall(null);
            activeCallRef.current = null;
            setIsInConference(false);
            isInConferenceRef.current = false;
            setAgentConference(null);
            agentConferenceRef.current = null;
            setConnectedCustomer(null);

            // Ensure backend marks the outbound call as ended so the socket
            // event fires and activeCalls gets cleaned up in the UI.
            // The call_state_webhook from SignalWire may be delayed or not arrive.
            const callId = outboundCallDbId || outboundDialCallIdRef.current;
            if (callId) {
              logger.debug('📞 [CallFabric] Marking outbound call as ended in backend:', callId);
              callsApi.updateStatus(callId, 'ended').catch(() => {});
            }
          });

          // Track the call
          const outboundCall: ActiveCall = {
            id: call.id,
            callerId: phoneNumber,
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

          setActiveCall(outboundCall);
          activeCallRef.current = outboundCall;
          conferenceCallRef.current = call;

          // Set conference info
          const conferenceInfo: Conference = {
            id: 0,
            conferenceName: confName,
            conferenceType: 'interaction',
            ownerUserId: user?.id || 0,
            status: 'active',
            createdAt: new Date().toISOString()
          };
          setAgentConference(conferenceInfo);
          agentConferenceRef.current = conferenceInfo;
          setCallState('ringing');

          // Start the call
          await call.start();

          // Fallback: mark active after start
          setTimeout(() => {
            if (!hasMarkedActive) {
              logger.debug('📞 [CallFabric] Fallback: marking outbound conf active');
              markActive();
            }
          }, 1000);

          // Wait for conference join to complete
          let attempts = 0;
          const maxAttempts = 20; // 10 seconds
          while (!isInConferenceRef.current && attempts < maxAttempts) {
            await new Promise(r => setTimeout(r, 500));
            attempts++;
          }

          if (!isInConferenceRef.current) {
            throw new Error('Failed to join conference - please try again');
          }

          logger.debug('✅ [CallFabric] In conference, dialing out...');
          const result = await dialOutToConference(confName, context?.contact_id);
          outboundCallDbId = result.call_id; // Store for cleanup in destroy handler
          return result;

        } finally {
          setIsChangingStatus(false);
        }
      }

      // Case 3: Agent is in some other state (busy, break, after-call)
      logger.error('❌ [CallFabric] Case 3: Invalid state for outbound call:', {
        status: currentAgentStatus,
        isInConference: currentIsInConference,
        hasConference: !!currentAgentConference,
        conferenceName: currentAgentConference?.conferenceName
      });

      throw new Error(`Cannot make outbound call while in ${currentAgentStatus} status. Please go available first.`);

    } catch (error: any) {
      logger.error('❌ [CallFabric] Failed to make call:', error);
      setError(error?.message || 'Failed to make call');
      setCallState('idle');
      throw error; // Re-throw so caller knows it failed
    }
  }, [user]); // Minimal dependencies - use refs for everything else

  // Hang up current call
  const hangup = useCallback(async () => {
    if (!activeCall) return;
    try {
      await activeCall.hangup();
      setActiveCall(null);
      setCallState('idle');
    } catch (error) {
      logger.error('Failed to hang up:', error);
    }
  }, [activeCall]);

  // Answer incoming call
  // When agent answers a server-initiated call, SignalWire fetches the SWML URL
  // which joins the agent to the conference automatically
  const answerCall = useCallback(async () => {
    const currentCall = activeCallRef.current;

    logger.debug('📞 [CallFabric] answerCall called');
    logger.debug('📞 [CallFabric] activeCall (from ref):', currentCall?.id);

    if (!currentCall || currentCall.direction !== 'inbound') {
      logger.debug('⚠️ [CallFabric] No inbound call to answer');
      return;
    }

    try {
      // Ensure microphone permission before accepting - this triggers the browser prompt
      // if permission hasn't been granted yet
      logger.debug('📞 [CallFabric] Requesting microphone permission...');
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(track => track.stop()); // Release immediately
        logger.debug('✅ [CallFabric] Microphone permission granted');
      } catch (micError) {
        logger.error('❌ [CallFabric] Microphone permission denied:', micError);
        setError('Microphone access required to answer calls');
        return;
      }

      logger.debug('📞 [CallFabric] Accepting incoming call...');
      const call = await currentCall.answer();
      logger.debug('✅ [CallFabric] Call answered, call object:', call);

      setCallState('active');

      // If we have a pending assignment, set up conference tracking
      const assignment = pendingCallAssignmentRef.current;
      if (assignment) {
        logger.debug('📞 [CallFabric] Setting conference state from assignment:', assignment.conferenceName);
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
      logger.error('❌ [CallFabric] Failed to answer call:', error);
    }
  }, [user]);

  // Join an interaction conference (per-interaction model)
  // Called when agent accepts a call assignment - dials into the interaction conference
  // where the customer is already waiting
  const joinInteractionConference = useCallback(async (dialAddress: string, conferenceName: string) => {
    if (!client || !user) {
      logger.debug('⚠️ [CallFabric] Cannot join conference - client or user not ready');
      throw new Error('Client or user not ready');
    }

    if (isInConference) {
      logger.debug('⚠️ [CallFabric] Already in a conference');
      throw new Error('Already in a conference');
    }

    try {
      logger.debug('📞 [CallFabric] Joining interaction conference:', conferenceName);
      logger.debug('📞 [CallFabric] Dialing:', dialAddress);

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
        logger.debug('📞 [CallFabric] Interaction conference call state:', state);
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
        logger.debug('📞 [CallFabric] Interaction conference call destroyed');
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

      logger.debug('✅ [CallFabric] Joined interaction conference:', conferenceName);

    } catch (error) {
      logger.error('❌ [CallFabric] Failed to join interaction conference:', error);
      setError('Failed to join conference');
      throw error;
    }
  }, [client, user, isInConference, socket]);

  // Make a call to a SWML resource (for takeover — connects agent to existing call)
  const makeCallToSwml = useCallback(async (swmlUrl: string, context?: any) => {
    if (!client) {
      setError('Phone system not initialized');
      return;
    }

    try {
      logger.debug('📞 [CallFabric] Making SWML call to:', swmlUrl);
      setCallState('ringing');

      // Track the original call SID for socket-based cleanup
      if (context?.original_call_sid) {
        takeoverCallSidRef.current = context.original_call_sid;
        logger.debug('📞 [CallFabric] Tracking takeover for call SID:', context.original_call_sid);
      }

      const call = await client.dial({
        to: swmlUrl,
        rootElement: rootElementRef.current,
        audio: true,
        video: false,
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user?.id,
          agent_name: user?.name,
          call_type: 'takeover',
          ...context
        }
      });

      let hasMarkedActive = false;
      const markCallActive = () => {
        if (hasMarkedActive) return;
        hasMarkedActive = true;
        logger.debug('✅ [CallFabric] Takeover call ACTIVE');
        setCallState('active');
      };

      const cleanupCall = () => {
        logger.debug('📞 [CallFabric] SWML call cleanup');
        setActiveCall(null);
        activeCallRef.current = null;
        setCallState('idle');
        takeoverCallSidRef.current = null;
      };

      const connectedStates = ['active', 'answered', 'answering', 'early', 'trying'];

      call.on('call.state', (state: any) => {
        logger.debug('📞 [CallFabric] SWML call state:', state);
        if (connectedStates.includes(state)) {
          markCallActive();
        } else if (state === 'ended' || state === 'hangup' || state === 'destroy') {
          cleanupCall();
        }
      });

      call.on('call.joined', () => {
        logger.debug('📞 [CallFabric] SWML call joined');
        markCallActive();
      });

      call.on('destroy', () => {
        logger.debug('📞 [CallFabric] SWML call destroyed');
        cleanupCall();
      });

      setActiveCall(call);
      activeCallRef.current = call;
      await call.start();

      logger.debug('✅ [CallFabric] SWML call started');
      return call;
    } catch (error) {
      logger.error('❌ [CallFabric] Failed to make SWML call:', error);
      setError('Failed to connect to call');
      setCallState('idle');
      throw error;
    }
  }, [client, user]);

  // Leave current conference (works for both agent and interaction conferences)
  const leaveConference = useCallback(async () => {
    if (!isInConference || !conferenceCallRef.current) {
      return;
    }

    try {
      logger.debug('📞 [CallFabric] Leaving conference...');

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
      logger.debug('✅ [CallFabric] Left conference');
    } catch (error) {
      logger.error('❌ [CallFabric] Failed to leave conference:', error);
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
      logger.debug('⚠️ [CallFabric] No pending call assignment to accept');
      throw new Error('No pending call assignment to accept');
    }

    if (!client || !user) {
      logger.debug('⚠️ [CallFabric] Cannot accept assignment - client or user not ready');
      throw new Error(`Cannot accept: ${!client ? 'Call Fabric client not connected' : 'user not loaded'}`);
    }

    const { conferenceName, callDbId, assignmentType, context, whisperMode, targetAgentCallSid, transport } = pendingCallAssignment;

    // Bridge mode: the backend has already initiated a server-side dial
    // TO our subscriber resource (via dial_to_queue_pickup). The SDK's
    // `incomingCallHandlers.all` should have already fired and stashed
    // the invite in `inviteRef.current`. Accept that — DO NOT call
    // prepareJoin or dial out to a conference (there is no conference in
    // bridge mode; conferenceName is empty here). See CALL_TRANSPORT.md.
    if (transport === 'bridge') {
      // Bridge mode timing: the Socket.IO `call_assignment` event reaches the
      // browser the moment the backend builds the SWML response, but the
      // SWML still has to traverse:
      //   SignalWire receives SWML → parses connect verb → starts dial →
      //   reaches subscriber's WebRTC binding → SDK fires
      //   incomingCallHandlers.all → inviteRef.current is set
      // Real-world this is 500ms–3s. Throwing "click again" when the user
      // clicks Accept faster than the SDK invite arrives is bad UX.
      // Instead, await the invite with a short timeout; if it never shows,
      // THEN surface the error.
      const WAIT_TIMEOUT_MS = 5000;
      const POLL_MS = 100;
      let waited = 0;
      while (!inviteRef.current && waited < WAIT_TIMEOUT_MS) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        waited += POLL_MS;
      }
      const invite = inviteRef.current;
      if (!invite) {
        // After 5s of waiting the SDK still hasn't seen an invite — most
        // likely the server-side dial failed (unresolvable Fabric address,
        // SignalWire 422, network) so no real call ever rang the device.
        // Surface a clear error rather than silent-fail.
        throw new Error(
          `Incoming call invite never arrived from SignalWire (waited ${WAIT_TIMEOUT_MS}ms). ` +
          'The server-side dial likely failed. Check backend logs for the connect target.'
        );
      }
      logger.debug(`📞 [CallFabric] Accepting BRIDGE invite (waited ${waited}ms for SDK invite)`);
      try {
        const call = await invite.accept({
          rootElement: rootElementRef.current,
          audio: true,
          video: false,
        });
        // Mirror what the SDK's own incomingCallHandlers.all does on accept,
        // so downstream state is consistent regardless of whether the
        // agent clicked the SDK's native UI or our banner.
        if (call) {
          (window as any).__swCall = call;
          (window as any).__swInvite = invite;
          activeCallRef.current = call as any;
          setActiveCall(call as any);
          setCallState('active');
          // Promote the backend Call row to 'active' so dashboards reflect
          // the live state. callDbId is the integer DB id.
          if (callDbId) {
            try {
              await callsApi.updateStatus(callDbId, 'active');
            } catch (err) {
              logger.warn('Failed to update bridge call status to active:', err);
            }
          }
          // Clear the pending assignment + invite refs — accepted.
          setPendingCallAssignment(null);
          inviteRef.current = null;
        }
      } catch (err) {
        logger.error('❌ [CallFabric] Bridge invite accept failed:', err);
        throw err;
      }
      return;
    }

    logger.debug(`📞 [CallFabric] Accepting ${assignmentType || 'normal'} call assignment via DIAL-OUT:`, conferenceName);

    try {
      // Step 1: Prepare the join by storing params in Redis (more reliable than query params)
      logger.debug('📞 [CallFabric] Preparing conference join via API...');
      const prepareParams: Parameters<typeof conferencesApi.prepareJoin>[0] = {
        agent_id: user.id,
        conference_name: conferenceName,
        call_id: callDbId,
      };

      // For backup/escalation, pass the join type and context for SWML mode switching + whisper
      if (assignmentType === 'backup' || assignmentType === 'escalation') {
        prepareParams.type = assignmentType;
        if (context && Object.keys(context).length > 0) {
          prepareParams.context = context;
        }
        if (whisperMode) {
          prepareParams.whisper_mode = true;
          if (targetAgentCallSid) {
            prepareParams.agent_call_sid = targetAgentCallSid;
          }
        }
      } else if (context && Object.keys(context).length > 0) {
        // Normal assignment with AI context — still enable whisper
        prepareParams.context = context;
      }

      const prepareResponse = await conferencesApi.prepareJoin(prepareParams);

      const dialAddress = prepareResponse.data.dial_address;
      logger.debug('📞 [CallFabric] Got dial address from API:', dialAddress);

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
        logger.debug('✅ [CallFabric] Marking call as ACTIVE');
        setCallState('active');
        // Update backend call status to 'active'
        if (dbCallIdForHandlers) {
          try {
            await callsApi.updateStatus(dbCallIdForHandlers, 'active');
            logger.debug('✅ [CallFabric] Updated call status to active in backend');
          } catch (err) {
            logger.error('❌ [CallFabric] Failed to update call status:', err);
          }
        }
      };

      // Set up call event handlers
      call.on('call.state', async (state: any) => {
        logger.debug('📞 [CallFabric] Conference call state:', state);
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
        logger.debug('📞 [CallFabric] Call joined event');
        await markCallActive();
      });

      call.on('call.updated', async (params: any) => {
        logger.debug('📞 [CallFabric] Call updated:', params);
        // If we get updated event, call is likely connected
        if (params?.state === 'active' || params?.node_id) {
          await markCallActive();
        }
      });

      call.on('destroy', async () => {
        logger.debug('📞 [CallFabric] Conference call destroyed');
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
          logger.debug('📞 [CallFabric] Fallback: marking call active after start() completed');
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
        logger.debug('📋 [CallFabric] Set connectedCustomer with AI context:', pendingCallAssignment.context);

        // Also call the callback if set (for navigation/additional handling)
        if (onCustomerConnectedRef.current) {
          onCustomerConnectedRef.current(customer);
        }
      }

      logger.debug('✅ [CallFabric] Dialing out to conference...');

      // Clear the pending assignment
      setPendingCallAssignment(null);
    } catch (error) {
      logger.error('❌ [CallFabric] Failed to accept call assignment:', error);
      throw error;
    }
  }, [pendingCallAssignment, user, client, socket]);

  // Accept a call assignment with explicit data (for taking calls from queue)
  const acceptCallAssignmentWithData = useCallback(async (assignment: Partial<CallAssignment>) => {
    if (!assignment.conferenceName) {
      logger.debug('⚠️ [CallFabric] No conference name in assignment data');
      return;
    }

    if (!client || !user) {
      logger.debug('⚠️ [CallFabric] Cannot accept assignment - client or user not ready');
      return;
    }

    const conferenceName = assignment.conferenceName;
    logger.debug('📞 [CallFabric] Accepting call assignment with data via DIAL-OUT:', conferenceName);

    try {
      // Step 1: Prepare the join by storing params in Redis (more reliable than query params)
      logger.debug('📞 [CallFabric] Preparing conference join via API...');
      const prepareResponse = await conferencesApi.prepareJoin({
        agent_id: user.id,
        conference_name: conferenceName,
        call_id: assignment.callDbId
      });

      const dialAddress = prepareResponse.data.dial_address;
      logger.debug('📞 [CallFabric] Got dial address from API:', dialAddress);

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
        logger.debug('✅ [CallFabric] Marking call as ACTIVE (with data)');
        setCallState('active');
        // Update backend call status to 'active'
        if (dbCallIdForStatusUpdate) {
          try {
            await callsApi.updateStatus(dbCallIdForStatusUpdate, 'active');
            logger.debug('✅ [CallFabric] Updated call status to active in backend');
          } catch (err) {
            logger.error('❌ [CallFabric] Failed to update call status:', err);
          }
        }
      };

      // Set up call event handlers
      call.on('call.state', async (state: any) => {
        logger.debug('📞 [CallFabric] Conference call state:', state);
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
        logger.debug('📞 [CallFabric] Call joined event');
        await markCallActive();
      });

      call.on('call.updated', async (params: any) => {
        logger.debug('📞 [CallFabric] Call updated:', params);
        // If we get updated event, call is likely connected
        if (params?.state === 'active' || params?.node_id) {
          await markCallActive();
        }
      });

      call.on('destroy', async () => {
        logger.debug('📞 [CallFabric] Conference call destroyed');
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
          logger.debug('📞 [CallFabric] Fallback: marking call active after start() completed');
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
        logger.debug('📋 [CallFabric] Set connectedCustomer with AI context:', assignment.context);

        // Also call the callback if set (for navigation/additional handling)
        if (onCustomerConnectedRef.current) {
          onCustomerConnectedRef.current(customer);
        }
      }

      logger.debug('✅ [CallFabric] Dialing out to conference...');

      // Clear any pending assignment
      setPendingCallAssignment(null);
    } catch (error) {
      logger.error('❌ [CallFabric] Failed to accept call assignment:', error);
      throw error;
    }
  }, [user, client, socket]);

  // Reject a call assignment - customer remains in queue
  const rejectCallAssignment = useCallback(() => {
    if (!pendingCallAssignment) {
      return;
    }

    logger.debug('📞 [CallFabric] Rejecting call assignment:', pendingCallAssignment.conferenceName);

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
    pendingCallAssignmentRef.current = pendingCallAssignment;
  }, [pendingCallAssignment]);

  useEffect(() => {
    clientRef.current = client;
  }, [client]);

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

  // Auto-transition agent status based on call lifecycle
  // available → busy when call connects, busy → after-call when call ends
  useEffect(() => {
    if (callState === 'active' && agentStatus === 'available') {
      logger.debug('🔄 [CallFabric] Auto-transition: available → busy (call active)');
      setAgentStatus('busy');
    } else if (callState === 'idle' && agentStatus === 'busy') {
      logger.debug('🔄 [CallFabric] Auto-transition: busy → after-call (call ended)');
      setAgentStatus('after-call');
    }
  }, [callState, agentStatus]);

  // Handle auto-restore when client is initialized
  // This handles page refresh: if agent was 'available', restore their online status
  // In per-interaction model, we just go online - no conference join until call assignment
  useEffect(() => {
    if (!client || !isClientReady || hasAttemptedAutoRejoinRef.current) {
      return;
    }

    if (agentStatus !== 'available') {
      logger.debug('📦 [CallFabric] Client ready, no auto-restore needed (status:', agentStatus, ')');
      return;
    }

    logger.debug('🔄 [CallFabric] Client ready, auto-restoring available status...');
    hasAttemptedAutoRejoinRef.current = true;

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
        logger.debug('✅ [CallFabric] Auto-restore successful - now available for call assignments');
        setConferenceJoinError(null);
      } catch (error: any) {
        logger.error('❌ [CallFabric] Auto-restore failed:', error);
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
      logger.debug('📥 [CallFabric] Status from server:', data);
      // Only update if not currently changing
      if (!isChangingStatus) {
        setAgentStatusState(data.status);
        // Note: Auto-rejoin is handled by the main useEffect that watches isClientReady
        // Just update state here - the useEffect will trigger rejoin if needed
      }
    };

    const handleAgentStatusUpdated = (data: { status: AgentStatusType }) => {
      logger.debug('✅ [CallFabric] Status confirmed:', data);
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
        logger.debug('🔄 [CallFabric] Socket connected, fetching persisted status...');
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
      logger.debug('📥 [CallFabric] Participant joined:', data);

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
      logger.debug('📥 [CallFabric] Participant left:', data);

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
      logger.debug('📥 [CallFabric] Customer routed to conference:', data);
      logger.debug('📥 [CallFabric] Current agent conference (ref):', agentConferenceRef.current?.conferenceName);

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

        logger.debug('🔔 Customer connected:', data.customer_info.name || data.customer_info.phone);
        logger.debug('📋 AI Context:', aiContext);
      } else {
        logger.debug('⚠️ [CallFabric] Conference name mismatch or no conference. Expected:', currentConference?.conferenceName, 'Got:', data.conference_name);
      }
    };

    // Handle conference status update
    const handleConferenceUpdate = (data: {
      conference_id: number;
      conference_name: string;
      status: 'active' | 'ended';
      participant_count: number;
    }) => {
      logger.debug('📥 [CallFabric] Conference update:', data);

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
    const handleCallAssignment = (data: any) => {
      logger.debug('📥 [CallFabric] Call assignment received:', data);

      // Detect if this is a backup/escalation assignment from call_control.py
      // vs a normal queue assignment from callcenter_socketio.py
      const isMultiAgent = data.type === 'backup' || data.type === 'escalation';

      let assignment: CallAssignment;

      if (isMultiAgent) {
        // Backup or escalation assignment — different payload shape
        const callData = data.call || {};
        logger.debug(`📞 [CallFabric] ${data.type} assignment: conference ${data.conference_name}`);
        logger.debug('👤 Requesting agent:', data.requesting_agent);

        assignment = {
          callId: callData.signalwire_call_sid || callData.id?.toString() || '',
          callDbId: data.call_db_id || callData.id,
          callerNumber: callData.from_number || '',
          queueId: callData.queue_id || '',
          context: callData.ai_context || {},
          agentId: 0,  // Will be set by the accepting agent
          agentName: '',
          conferenceName: data.conference_name,
          customerInfo: {
            phone: callData.from_number || '',
            name: callData.contact_name || callData.customer_name,
          },
          // Multi-agent specific fields
          assignmentType: data.type,
          requestingAgent: data.requesting_agent,
          whisperMode: data.whisper_mode || false,
          targetAgentCallSid: data.agent_call_sid,
          legId: data.leg_id,
        };
      } else {
        // Normal queue assignment
        logger.debug('📞 Conference:', data.conference_name);
        logger.debug('👤 Customer:', data.customer_info);
        logger.debug('📱 Agent call SID:', data.agent_call_sid);

        assignment = {
          callId: data.call_id,
          callDbId: data.call_db_id,
          callerNumber: data.caller_number,
          queueId: data.queue_id,
          context: data.context,
          agentId: data.agent_id,
          agentName: data.agent_name,
          conferenceName: data.conference_name,
          agentCallSid: data.agent_call_sid,
          customerInfo: data.customer_info,
          assignmentType: 'normal',
          // Bridge mode emits transport='bridge' so the accept handler
          // can take the SDK-invite path instead of the conference dial-in
          // path. Defaults to 'conference' for backward compat.
          transport: (data.transport === 'bridge' ? 'bridge' : 'conference'),
        };
      }

      setPendingCallAssignment(assignment);

      // The incoming call from the server will trigger the 'ringing' state
      // UI will show call info from this assignment + standard answer/reject buttons
      logger.debug(`🔔 [CallFabric] Call assignment received (${assignment.assignmentType || 'normal'})`);
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

  // When the agent leaves a conference, ensure any tracked outbound dial call
  // is marked as ended in the backend. This handles all conference types (Case 1
  // where agent was already in conference, Case 2 outbound conference, queue calls, etc.)
  useEffect(() => {
    if (!isInConference && outboundDialCallIdRef.current) {
      const callId = outboundDialCallIdRef.current;
      logger.debug('📞 [CallFabric] Conference ended, marking outbound dial call as ended:', callId);
      callsApi.updateStatus(callId, 'ended').catch(() => {});
      outboundDialCallIdRef.current = null;
    }
  }, [isInConference]);

  // Socket-based cleanup for calls where the Call Fabric SDK doesn't fire 'destroy'
  // This handles two scenarios:
  // 1. Takeover calls: SWML connect verb doesn't fire destroy when customer hangs up
  // 2. Outbound conference calls: customer hangs up their phone but agent's SDK leg stays open
  useEffect(() => {
    if (!socket) return;

    // Shared cleanup: hang up SDK call, reset all call/conference state
    const cleanupSdkState = (reason: string) => {
      logger.debug(`📞 [CallFabric] Socket cleanup (${reason}): resetting SDK state`);

      // Try to hang up the Call Fabric SDK call gracefully
      const currentCall = activeCallRef.current;
      if (currentCall) {
        try { currentCall.hangup(); } catch (e) { /* already ended */ }
      }

      // Reset call state
      setCallState('idle');
      setActiveCall(null);
      activeCallRef.current = null;
      outboundDialCallIdRef.current = null;

      // Reset conference state (for outbound conference calls)
      setIsInConference(false);
      isInConferenceRef.current = false;
      setAgentConference(null);
      agentConferenceRef.current = null;
      setConnectedCustomer(null);
      conferenceCallRef.current = null;
    };

    const handleCallEnded = (data: { callId: number; call_sid?: string; conference_name?: string; assigned_agent_id?: number }) => {
      // Match 1: Conference calls by conference name
      const currentConf = agentConferenceRef.current;
      if (currentConf && data.conference_name && data.conference_name === currentConf.conferenceName) {
        cleanupSdkState(`call_ended: conference ${currentConf.conferenceName}`);
        return;
      }

      // Match 2: Outbound dialed calls by DB ID
      if (outboundDialCallIdRef.current && data.callId === outboundDialCallIdRef.current) {
        cleanupSdkState('call_ended: outbound dial');
        return;
      }
    };

    const handleCallUpdate = (data: { call: any }) => {
      const call = data.call;
      if (!call) return;

      const isEnded = ['ended', 'completed'].includes(call.status);
      if (!isEnded) return;

      // Match 1: Conference calls by conference name (outbound calls, queue calls, takeover)
      // When the customer's call ends, its conference_name matches our active conference
      const currentConf = agentConferenceRef.current;
      if (currentConf && call.conference_name === currentConf.conferenceName) {
        cleanupSdkState(`conference call ended: ${currentConf.conferenceName}`);
        return;
      }

      // Match 2: Outbound dialed call by DB ID
      // This handles the case where the SDK already fired 'destroy' (clearing
      // activeCallRef and agentConferenceRef) before the socket event arrives
      if (outboundDialCallIdRef.current && call.id === outboundDialCallIdRef.current) {
        cleanupSdkState(`outbound dial call_update ended: ${call.id}`);
        return;
      }
    };

    socket.on('call_ended', handleCallEnded);
    socket.on('call_update', handleCallUpdate);

    return () => {
      socket.off('call_ended', handleCallEnded);
      socket.off('call_update', handleCallUpdate);
    };
  }, [socket]);

  // Note: We intentionally do NOT auto-restore 'available' status on page load
  // Agent must explicitly go available when ready to take calls

  // Initialize client on mount, clean up on unmount
  useEffect(() => {
    if (user && !client && !initializingRef.current) {
      initializeClient();
    }

    // Hard-refresh / tab close cleanup. React unmount alone isn't enough —
    // Ctrl+Shift+R kills the page synchronously before lifecycle hooks fire,
    // leaving SignalWire's side still considering the subscriber registered.
    // The next page load fails to register with "device already registered
    // for push notifications" until SignalWire's session timeout elapses.
    // beforeunload fires BEFORE the page navigates away, giving us a chance
    // to tell SignalWire we're going. Best-effort — the request may not
    // complete before the page dies, but it's strictly better than nothing.
    const onBeforeUnload = () => {
      const c = clientRef.current;
      if (c) {
        try {
          // Don't await — page is about to die. Just kick off the request.
          c.offline().catch(() => {});
        } catch {
          // Ignore
        }
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    window.addEventListener('pagehide', onBeforeUnload); // mobile/iOS fallback

    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
      window.removeEventListener('pagehide', onBeforeUnload);
      // Properly disconnect the SDK client to prevent orphaned WebRTC connections
      // This is critical during Vite HMR — without cleanup, old RTCPeerConnections
      // linger and cause "_sdpReady called in wrong state: closed" errors
      const currentClient = clientRef.current;
      if (currentClient) {
        logger.debug('🧹 [CallFabric] Cleaning up SDK client on unmount');
        try {
          currentClient.offline().catch(() => {});
        } catch (e) {
          // Ignore cleanup errors
        }
      }
      if (rootElementRef.current) {
        rootElementRef.current.remove();
        rootElementRef.current = null;
      }
      // Reset initialization ref so re-mount can initialize again
      initializingRef.current = false;
    };
  }, [user]);

  // Helper functions for connected customer
  const setOnCustomerConnected = useCallback((callback: ((customer: ConnectedCustomer) => void) | undefined) => {
    onCustomerConnectedRef.current = callback;
  }, []);

  const clearConnectedCustomer = useCallback(() => {
    setConnectedCustomer(null);
  }, []);

  // Recovery for "WebRTC endpoint registration failed" (SignalWire error
  // -32603). Clears the SDK's persisted subscriber state in localStorage
  // (it stores everything under `sw:*` keys per the registration-runtime
  // spec), tries to politely offline any still-connected client, and
  // reloads the page so the next mount gets a fresh `initializeClient` →
  // `getSubscriberToken` → `client.online()` sequence.
  //
  // Surfaced in the agent header as a "Reset" button when
  // `registrationError` is set.
  const resetCallFabricState = useCallback(async () => {
    logger.warn('🔄 [CallFabric] resetCallFabricState invoked — clearing cached SDK state');
    // Best-effort offline. The SDK's offline() can hang if the WebSocket
    // is already dead, so race it against a short timeout.
    try {
      const c = clientRef.current;
      if (c) {
        await Promise.race([
          c.offline().catch(() => {}),
          new Promise((resolve) => setTimeout(resolve, 800)),
        ]);
      }
    } catch (e) {
      logger.debug('🔄 [CallFabric] offline() during reset failed (non-fatal):', e);
    }
    // Nuke every SDK-owned localStorage key. The SDK stores its protocol-
    // type binding under `sw:{subscriberId}:pt`; clearing all `sw:` keys
    // ensures a clean slate even across subscriber switches.
    try {
      const keys = Object.keys(localStorage).filter((k) => k.startsWith('sw:'));
      for (const k of keys) localStorage.removeItem(k);
      logger.warn(`🔄 [CallFabric] Cleared ${keys.length} sw:* localStorage keys`);
    } catch (e) {
      logger.warn('🔄 [CallFabric] localStorage clear failed:', e);
    }
    // Hard reload bypasses any in-memory module caches and forces a fresh
    // SDK initialization + token mint.
    window.location.reload();
  }, []);

  const value: CallFabricContextType = {
    client,
    activeCall,
    isOnline,
    isInitializing,
    isClientReady,
    error,
    registrationError,
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
    hangup,
    answerCall,
    requestMicPermission,
    // Call-method dispatch. `activeCall` can be either (a) the raw CF SDK
    // Call from `client.dial(...)` or (b) a wrapped shim we construct for
    // outbound calls. The raw SDK exposes audioMute/audioUnmute; the shim
    // exposes mute/unmute.
    //
    // Conference-joined calls (dial to a SWML resource that ends in
    // join_conference) on @signalwire/client@dev throw
    // ``CapabilityError: Missing audio mute capability`` from audioMute() —
    // the SDK's per-call capability set doesn't include audio_mute when the
    // call landed through SWML. We don't get to flip that flag from the
    // client side, and the conference participant verb didn't grant it.
    //
    // Fallback chain: try every SDK method we can spot, then drop down to
    // toggling the RTCRtpSender.track.enabled flag directly. WebRTC mutes
    // by zeroing the outgoing audio frames, which is exactly what the SDK's
    // happy-path audioMute does under the hood — we're just doing it
    // ourselves when the SDK refuses.
    mute: async () => setLocalAudioMuted(activeCall, true, setIsMuted),
    unmute: async () => setLocalAudioMuted(activeCall, false, setIsMuted),
    hold: async () => { await activeCall?.hold?.(); },
    unhold: async () => { await activeCall?.unhold?.(); },
    sendDigits: async (digits: string) => { await activeCall?.sendDigits?.(digits); },
    // Conference actions (per-interaction model)
    joinInteractionConference,
    leaveConference,
    // Recovery
    resetCallFabricState,
    // Takeover calls
    makeCallToSwml,
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
