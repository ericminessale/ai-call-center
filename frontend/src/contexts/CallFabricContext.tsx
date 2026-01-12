import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { useAuthStore } from '../stores/authStore';
import { useSocketContext } from './SocketContext';

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

  // Agent status state
  const [agentStatus, setAgentStatusState] = useState<AgentStatusType>('offline');
  const [isChangingStatus, setIsChangingStatus] = useState(false);

  // ICE gathering readiness - client exists but may not be usable until ICE completes (~10s)
  const [isClientReady, setIsClientReady] = useState(false);
  const pendingStatusRef = useRef<AgentStatusType | null>(null);
  const iceReadyTimerRef = useRef<NodeJS.Timeout | null>(null);

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

  // Load SignalWire SDK
  const loadSignalWireSDK = () => {
    return new Promise((resolve, reject) => {
      if (window.SignalWire) {
        resolve(true);
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://unpkg.com/@signalwire/client@dev';
      script.async = true;
      script.onload = resolve;
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
      console.log('✅ [CallFabric] Client initialized, waiting for ICE gathering...');

      // ICE gathering takes ~10 seconds - wait before marking client as ready
      // This prevents "No valid pooled connections available" errors
      const ICE_READY_DELAY = 10500; // 10.5 seconds
      iceReadyTimerRef.current = setTimeout(() => {
        console.log('✅ [CallFabric] ICE gathering complete, client is now ready');
        setIsClientReady(true);
        // The useEffect watching isClientReady will execute any pending status change
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
      } else if (newStatus === 'offline' || newStatus === 'break') {
        if (isOnline) {
          await goOffline();
        }
      }
      // 'busy' and 'after-call' keep Call Fabric online but remove from queue

      // Update Redis status
      updateRedisStatus(newStatus);

      // Update local state
      setAgentStatusState(newStatus);
      console.log('✅ [CallFabric] Status changed to:', newStatus);

    } catch (error) {
      console.error('❌ [CallFabric] Failed to change status:', error);
      setError('Failed to change status');
    } finally {
      setIsChangingStatus(false);
    }
  }, [client, isClientReady, isOnline, socket, connectionStatus, goOnline, goOffline, updateRedisStatus]);

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
  const makeCall = useCallback(async (phoneNumber: string, context?: any) => {
    if (!client) {
      setError('Phone system not initialized');
      return;
    }

    if (!isClientReady) {
      setError('Phone system still initializing, please wait a moment');
      return;
    }

    try {
      console.log('📞 [CallFabric] Making call to:', phoneNumber);
      setCallState('ringing');

      const call = await client.dial({
        to: phoneNumber,
        rootElement: rootElementRef.current,
        logLevel: 'debug',
        debug: { logWsTraffic: true },
        userVariables: {
          agent_id: user?.id,
          agent_name: user?.name,
          call_type: 'outbound',
          context: context
        }
      });

      call.on('call.state', (state: any) => {
        if (state === 'active' || state === 'answered') setCallState('active');
        else if (state === 'ending' || state === 'ended') setCallState('ending');
      });

      call.on('destroy', () => {
        setActiveCall(null);
        setCallState('idle');
      });

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
      await call.start();
      return call;

    } catch (error) {
      console.error('❌ [CallFabric] Failed to make call:', error);
      setError('Failed to make call');
      setCallState('idle');
    }
  }, [client, isClientReady, user]);

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

  // Listen for socket status updates
  useEffect(() => {
    if (!socket) return;

    const handleAgentStatus = (data: { status: AgentStatusType }) => {
      console.log('📥 [CallFabric] Status from server:', data);
      // Only update if not currently changing
      if (!isChangingStatus) {
        setAgentStatusState(data.status);
      }
    };

    const handleAgentStatusUpdated = (data: { status: AgentStatusType }) => {
      console.log('✅ [CallFabric] Status confirmed:', data);
      setAgentStatusState(data.status);
      setIsChangingStatus(false);
    };

    socket.on('agent_status', handleAgentStatus);
    socket.on('agent_status_updated', handleAgentStatusUpdated);

    // Get initial status
    if (connectionStatus === 'connected') {
      const token = localStorage.getItem('access_token');
      if (token) {
        socket.emit('get_agent_status', { token });
      }
    }

    return () => {
      socket.off('agent_status', handleAgentStatus);
      socket.off('agent_status_updated', handleAgentStatusUpdated);
    };
  }, [socket, connectionStatus, isChangingStatus]);

  // Execute pending status change when client becomes ready
  useEffect(() => {
    if (isClientReady && pendingStatusRef.current) {
      const pendingStatus = pendingStatusRef.current;
      console.log('📱 [CallFabric] Client ready, executing pending status:', pendingStatus);
      pendingStatusRef.current = null;

      // Execute the pending status change
      (async () => {
        try {
          if (pendingStatus === 'available' && !isOnline) {
            await goOnline();
          }
          updateRedisStatus(pendingStatus);
          setAgentStatusState(pendingStatus);
          console.log('✅ [CallFabric] Pending status executed:', pendingStatus);
        } catch (error) {
          console.error('❌ [CallFabric] Failed to execute pending status:', error);
          setError('Failed to go online');
        } finally {
          setIsChangingStatus(false);
        }
      })();
    }
  }, [isClientReady, isOnline, goOnline, updateRedisStatus]);

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
    sendDigits: async (digits: string) => { await activeCall?.sendDigits(digits); }
  };

  return (
    <CallFabricContext.Provider value={value}>
      {children}
    </CallFabricContext.Provider>
  );
}
