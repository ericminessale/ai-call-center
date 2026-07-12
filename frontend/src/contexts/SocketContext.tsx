import React, { createContext, useContext, useEffect, useRef, useState, ReactNode } from 'react';
import { io, Socket } from 'socket.io-client';
import { useAuthStore } from '../stores/authStore';
import { socketService } from '../services/api';
import { logger } from '../lib/logger';

interface SocketContextType {
  socket: Socket | null;
  connectionStatus: 'connected' | 'disconnected' | 'reconnecting';
}

const SocketContext = createContext<SocketContextType>({
  socket: null,
  connectionStatus: 'disconnected',
});

export const useSocketContext = () => useContext(SocketContext);

interface SocketProviderProps {
  children: ReactNode;
}

export function SocketProvider({ children }: SocketProviderProps) {
  const socketRef = useRef<Socket | null>(null);
  const [socket, setSocket] = useState<Socket | null>(null);
  const { isAuthenticated } = useAuthStore();
  const [connectionStatus, setConnectionStatus] = useState<'connected' | 'disconnected' | 'reconnecting'>('disconnected');

  useEffect(() => {
    if (!isAuthenticated) {
      if (socketRef.current) {
        logger.debug('[Socket] User logged out, disconnecting');
        socketRef.current.disconnect();
        socketRef.current = null;
        setSocket(null);
      }
      setConnectionStatus('disconnected');
      return;
    }

    const token = localStorage.getItem('access_token');
    if (!token) return;

    if (!socketRef.current) {
      logger.debug('[Socket] Creating connection');
      const newSocket = io('/', {
        path: '/socket.io/',
        transports: ['websocket', 'polling'],
        // auth as a CALLBACK so every (re)connection handshakes with the
        // CURRENT token. A captured token object goes stale after a JWT
        // refresh, and since Phase 3 every server emit is room-scoped —
        // a reconnect that fails auth would silently receive nothing.
        auth: (cb) => cb({ token: localStorage.getItem('access_token') || '' }),
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        timeout: 20000,
      });

      socketRef.current = newSocket;
      setSocket(newSocket);

      newSocket.on('connect', () => {
        // Fires on every (re)connection — re-authenticate with the CURRENT
        // token so the server re-joins our user + workspace rooms.
        logger.debug('[Socket] Connected:', newSocket.id);
        setConnectionStatus('connected');
        const currentToken = localStorage.getItem('access_token') || token;
        newSocket.emit('authenticate', { token: currentToken });
      });

      newSocket.on('authenticated', (data) => {
        logger.debug('[Socket] Authenticated:', data);
      });

      newSocket.on('disconnect', (reason) => {
        logger.debug('[Socket] Disconnected:', reason);
        setConnectionStatus('disconnected');

        if (reason === 'io server disconnect') {
          newSocket.connect();
        }
      });

      // Reconnect lifecycle events fire on the MANAGER (socket.io v4), not
      // the socket — the previous socket-level listeners never fired.
      newSocket.io.on('reconnect_attempt', (attemptNumber) => {
        logger.debug('[Socket] Reconnecting, attempt', attemptNumber);
        setConnectionStatus('reconnecting');
      });

      newSocket.io.on('reconnect', (attemptNumber) => {
        // Room re-join happens in the 'connect' handler above; this is
        // status-only.
        logger.debug('[Socket] Reconnected after', attemptNumber, 'attempts');
        setConnectionStatus('connected');
      });

      newSocket.io.on('reconnect_error', (error) => {
        logger.error('[Socket] Reconnection error:', error);
      });

      newSocket.io.on('reconnect_failed', () => {
        logger.error('[Socket] Reconnection failed after all attempts');
        setConnectionStatus('disconnected');
      });

      newSocket.on('error', (error) => {
        logger.error('[Socket] Error:', error);
      });
    } else if (!socketRef.current.connected) {
      logger.debug('[Socket] Reconnecting existing socket');
      socketRef.current.connect();
    }

    return () => {
      // Socket persists across re-renders, cleaned up on logout
    };
  }, [isAuthenticated]);

  // Sync socket to socketService for legacy component support
  useEffect(() => {
    socketService.setSocket(socket);
  }, [socket]);

  return (
    <SocketContext.Provider value={{ socket, connectionStatus }}>
      {children}
    </SocketContext.Provider>
  );
}
