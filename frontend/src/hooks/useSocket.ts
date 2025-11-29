import { useEffect, useRef, useState } from 'react';
import { io, Socket } from 'socket.io-client';
import { useAuthStore } from '../stores/authStore';

export const useSocket = () => {
  const socketRef = useRef<Socket | null>(null);
  const [socket, setSocket] = useState<Socket | null>(null);
  const { isAuthenticated } = useAuthStore();
  const [connectionStatus, setConnectionStatus] = useState<'connected' | 'disconnected' | 'reconnecting'>('disconnected');
  const reconnectAttempts = useRef(0);

  useEffect(() => {
    console.log('🔌 [HOOK] useSocket effect running, isAuthenticated:', isAuthenticated);

    if (!isAuthenticated) {
      console.log('🔌 [HOOK] Not authenticated, skipping socket setup');
      // Only disconnect if we have a socket and user logged out
      if (socketRef.current) {
        console.log('🔌 [HOOK] User logged out, disconnecting socket');
        socketRef.current.disconnect();
        socketRef.current = null;
        setSocket(null);
      }
      setConnectionStatus('disconnected');
      return;
    }

    const token = localStorage.getItem('access_token');
    if (!token) {
      console.log('🔌 [HOOK] No token found, skipping socket setup');
      return;
    }

    console.log('🔌 [HOOK] Token exists, checking socket status');

    // Initialize socket connection
    if (!socketRef.current) {
      console.log('🔌 [HOOK] Creating new socket connection');
      const newSocket = io('/', {
        path: '/socket.io/',
        transports: ['websocket', 'polling'],
        auth: {
          token
        },
        // Enable automatic reconnection
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        timeout: 20000
      });

      socketRef.current = newSocket;
      setSocket(newSocket);

      // Connection event handlers
      newSocket.on('connect', () => {
        console.log('🔌 [SOCKET] WebSocket connected, socket.id:', newSocket.id);
        setConnectionStatus('connected');
        reconnectAttempts.current = 0;

        // Send authentication immediately after connecting
        newSocket.emit('authenticate', { token });
        console.log('🔌 [SOCKET] Sent authenticate event');
      });

      newSocket.on('authenticated', (data) => {
        console.log('🔌 [SOCKET] WebSocket authenticated:', data);
      });

      newSocket.on('disconnect', (reason) => {
        console.log('🔌 [SOCKET] WebSocket disconnected, reason:', reason);
        setConnectionStatus('disconnected');

        // If transport closed (backend restarted), force cleanup and reconnect
        if (reason === 'transport close' || reason === 'transport error') {
          console.log('🔌 [SOCKET] Backend likely restarted, forcing socket cleanup and reconnect...');
          // Disconnect completely
          newSocket.disconnect();
          // Clear the ref so a new socket will be created
          socketRef.current = null;
          setSocket(null);

          // Reconnect after a short delay
          setTimeout(() => {
            console.log('🔌 [SOCKET] Attempting to create fresh socket connection...');
            const token = localStorage.getItem('access_token');
            if (token) {
              // This will trigger the useEffect to run again since socket is now null
              window.location.reload();
            }
          }, 100);
        }
        // If server disconnected us, try to reconnect
        else if (reason === 'io server disconnect') {
          console.log('🔌 [SOCKET] Server disconnected us, manually reconnecting...');
          newSocket.connect();
        }
      });

      newSocket.on('reconnect_attempt', (attemptNumber) => {
        console.log('🔌 [SOCKET] Reconnection attempt #', attemptNumber);
        setConnectionStatus('reconnecting');
        reconnectAttempts.current = attemptNumber;
      });

      newSocket.on('reconnect', (attemptNumber) => {
        console.log('🔌 [SOCKET] Successfully reconnected after', attemptNumber, 'attempts');
        setConnectionStatus('connected');
        reconnectAttempts.current = 0;

        // Re-authenticate after reconnection
        const currentToken = localStorage.getItem('access_token');
        if (currentToken) {
          newSocket.emit('authenticate', { token: currentToken });
          console.log('🔌 [SOCKET] Re-authenticated after reconnection');
        }
      });

      newSocket.on('reconnect_error', (error) => {
        console.error('🔌 [SOCKET] Reconnection error:', error);
      });

      newSocket.on('reconnect_failed', () => {
        console.error('🔌 [SOCKET] Reconnection failed after all attempts');
        setConnectionStatus('disconnected');
      });

      newSocket.on('error', (error) => {
        console.error('🔌 [SOCKET] WebSocket error:', error);
      });

      console.log('🔌 [HOOK] Socket created and listeners registered');
    } else {
      console.log('🔌 [HOOK] Socket already exists, checking if connected');

      // If socket exists but is disconnected, reconnect it
      if (!socketRef.current.connected) {
        console.log('🔌 [HOOK] Socket exists but not connected, reconnecting...');
        socketRef.current.connect();
      }
    }

    // Don't cleanup the socket in development mode (React Strict Mode causes double mounting)
    // Socket will be reused across re-renders and only disconnected when user logs out
    return () => {
      console.log('🔌 [HOOK] Cleanup function called (socket will persist)');
    };
  }, [isAuthenticated]);

  return socket;
};