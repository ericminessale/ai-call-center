import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuthStore } from '../stores/authStore';

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

export const useCallFabric = () => {
  const [client, setClient] = useState<CallFabricClient | null>(null);
  const [activeCall, setActiveCall] = useState<ActiveCall | null>(null);
  const [isOnline, setIsOnline] = useState(false);
  const [isInitializing, setIsInitializing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [callState, setCallState] = useState<'idle' | 'ringing' | 'active' | 'ending'>('idle');
  const [isMuted, setIsMuted] = useState(false);
  const [micPermission, setMicPermission] = useState<'granted' | 'denied' | 'prompt' | 'unknown'>('unknown');

  const { user } = useAuthStore();
  const rootElementRef = useRef<HTMLDivElement | null>(null);
  const inviteRef = useRef<any>(null);
  const initializingRef = useRef(false);  // Sync guard against multiple initializations

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

  // Initialize Call Fabric client
  const initializeClient = async () => {
    // Use ref for synchronous guard - state updates are async and can miss rapid calls
    if (initializingRef.current || client) return;
    initializingRef.current = true;

    setIsInitializing(true);
    setError(null);

    try {
      // Load SignalWire SDK if not already loaded
      if (!window.SignalWire) {
        await loadSignalWireSDK();
      }

      const { SignalWire: SWire } = window.SignalWire;

      // Get token for this subscriber/agent
      const token = await getSubscriberToken();

      // Create root element for video/audio - must be visible for WebRTC to work
      if (!rootElementRef.current) {
        rootElementRef.current = document.createElement('div');
        rootElementRef.current.id = 'signalwire-root';
        // Position off-screen instead of display:none (WebRTC needs visible element)
        rootElementRef.current.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;';
        document.body.appendChild(rootElementRef.current);
      }

      // Initialize client
      const swClient = await SWire({
        token: token,
        host: import.meta.env.VITE_SIGNALWIRE_HOST,
        debug: {
          logWsTraffic: true
        },
        logLevel: 'debug'
      });

      // WORKAROUND: Wait for SDK connection pool to initialize before allowing calls
      // The SDK creates a pooled RTCPeerConnection and gathers ICE candidates at init.
      // If we dial before pool is ready, both pool and call gather ICE simultaneously,
      // causing a race condition where "Already processing local SDP" prevents verto.invite.
      // The pool ICE gathering times out after exactly 10 seconds.
      // TODO: Find SDK event to detect pool readiness instead of fixed delay
      console.log('Waiting for connection pool to initialize (10s)...');
      await new Promise(resolve => setTimeout(resolve, 10500));
      console.log('Connection pool ready');

      setClient(swClient);
      console.log('Call Fabric client initialized');

    } catch (error) {
      console.error('Failed to initialize Call Fabric client:', error);
      setError('Failed to initialize phone system');
      initializingRef.current = false;  // Allow retry on failure
    } finally {
      setIsInitializing(false);
    }
  };

  // Request microphone permission
  const requestMicPermission = useCallback(async (): Promise<boolean> => {
    try {
      // First check current permission status if API is available
      if (navigator.permissions && navigator.permissions.query) {
        const status = await navigator.permissions.query({ name: 'microphone' as PermissionName });
        console.log('Current mic permission status:', status.state);

        if (status.state === 'denied') {
          setMicPermission('denied');
          setError('Microphone permission denied. Please enable in browser settings.');
          return false;
        }
      }

      // Request actual mic access
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // Stop the stream immediately - we just needed to trigger the permission
      stream.getTracks().forEach(track => track.stop());

      setMicPermission('granted');
      console.log('Microphone permission granted');
      return true;
    } catch (err: any) {
      console.error('Microphone permission error:', err);

      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        setMicPermission('denied');
        setError('Microphone permission denied. Please enable in browser settings.');
      } else if (err.name === 'NotFoundError') {
        setMicPermission('denied');
        setError('No microphone found. Please connect a microphone.');
      } else {
        setMicPermission('denied');
        setError(`Microphone error: ${err.message}`);
      }
      return false;
    }
  }, []);

  // Go online to receive calls
  const goOnline = useCallback(async () => {
    if (!client || isOnline) return;

    try {
      // SDK will request mic permission when answering calls
      await client.online({
        incomingCallHandlers: {
          all: async (notification: any) => {
            console.log('Incoming call notification:', notification);

            // Store the invite for later
            inviteRef.current = notification.invite;

            // Extract context from call headers/variables
            const aiContext = notification.invite.details?.userVariables?.ai_context;
            const queueContext = notification.invite.details?.userVariables?.queue_context;

            // For call center agents, auto-answer could be optional
            const autoAnswer = localStorage.getItem('auto_answer') === 'true';

            setCallState('ringing');

            // Create call object
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
              hold: async () => { /* implement */ },
              unhold: async () => { /* implement */ },
              mute: async () => { /* implement */ },
              unmute: async () => { /* implement */ },
              sendDigits: async (digits: string) => { /* implement */ }
            };

            setActiveCall(incomingCall);

            // Auto-answer if enabled
            if (autoAnswer) {
              setTimeout(() => {
                incomingCall.answer();
              }, 1000); // 1 second delay
            }
          }
        }
      });

      setIsOnline(true);
      console.log('Agent is now online and ready to receive calls');

    } catch (error) {
      console.error('Failed to go online:', error);
      setError('Failed to go online');
    }
  }, [client, isOnline]);

  // Go offline
  const goOffline = useCallback(async () => {
    if (!client || !isOnline) return;

    try {
      await client.offline();
      setIsOnline(false);
      console.log('Agent is now offline');
    } catch (error) {
      console.error('Failed to go offline:', error);
    }
  }, [client, isOnline]);

  // Make outbound call
  const makeCall = useCallback(async (phoneNumber: string, context?: any) => {
    if (!client) {
      setError('Phone system not initialized');
      return;
    }

    try {
      // SDK handles mic permission automatically when dial() is called
      console.log('Making call to:', phoneNumber);

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

      console.log('Call initiated, call.id:', call.id);

      // Listen for call state changes
      call.on('call.state', (state: any) => {
        console.log('Call state changed:', state);
        if (state === 'active' || state === 'answered') {
          setCallState('active');
        } else if (state === 'ending' || state === 'ended') {
          setCallState('ending');
        }
      });

      call.on('destroy', () => {
        console.log('Call destroyed');
        setActiveCall(null);
        setCallState('idle');
      });

      // Create active call object
      const outboundCall: ActiveCall = {
        id: call.id,
        callerId: phoneNumber,
        direction: 'outbound',
        status: 'connecting',
        startTime: new Date(),
        answer: async () => { /* outbound calls don't need answer */ },
        hangup: async () => await call.hangup(),
        hold: async () => { /* implement */ },
        unhold: async () => { /* implement */ },
        mute: async () => await call.audioMute(),
        unmute: async () => await call.audioUnmute(),
        sendDigits: async (digits: string) => await call.sendDigits(digits)
      };

      setActiveCall(outboundCall);

      // Start the call - this actually initiates the WebRTC connection
      // Note: dial() just creates the call object, start() sends it to SignalWire
      console.log('Starting call...');
      await call.start();
      console.log('Call started');

      return call;

    } catch (error) {
      console.error('Failed to make call:', error);
      setError('Failed to make call');
      setCallState('idle');
    }
  }, [client, user]);

  // Make call to SWML URL (for takeover calls)
  const makeCallToSwml = useCallback(async (swmlUrl: string, context?: any) => {
    if (!client) {
      setError('Phone system not initialized');
      return;
    }

    try {
      console.log('Making SWML call to:', swmlUrl);
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
        console.log('SWML call state:', state);
        if (state === 'active' || state === 'answered') setCallState('active');
        else if (state === 'ending' || state === 'ended') setCallState('ending');
      });

      call.on('destroy', () => {
        console.log('SWML call destroyed');
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
      console.log('SWML call started');
      return call;

    } catch (error) {
      console.error('Failed to make SWML call:', error);
      setError('Failed to take over call');
      setCallState('idle');
    }
  }, [client, user]);

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

  // Load SignalWire SDK
  const loadSignalWireSDK = () => {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://unpkg.com/@signalwire/client@dev';
      script.async = true;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  };

  // Initialize on mount
  useEffect(() => {
    if (user && !client) {
      initializeClient();
    }

    return () => {
      if (rootElementRef.current) {
        rootElementRef.current.remove();
      }
    };
  }, [user]);

  // Auto go online when client is ready (optional)
  useEffect(() => {
    if (client && !isOnline) {
      const autoOnline = localStorage.getItem('auto_online') === 'true';
      if (autoOnline) {
        goOnline();
      }
    }
  }, [client, goOnline]);

  return {
    // State
    client,
    activeCall,
    isOnline,
    isInitializing,
    error,
    callState,
    isMuted,
    micPermission,

    // Actions
    initializeClient,
    goOnline,
    goOffline,
    makeCall,
    makeCallToSwml,
    hangup,
    answerCall,
    requestMicPermission,

    // Call controls
    mute: async () => {
      await activeCall?.mute();
      setIsMuted(true);
    },
    unmute: async () => {
      await activeCall?.unmute();
      setIsMuted(false);
    },
    hold: () => activeCall?.hold(),
    unhold: () => activeCall?.unhold(),
    sendDigits: (digits: string) => activeCall?.sendDigits(digits)
  };
};