import React, { createContext, useContext, useEffect, useRef, useState, ReactNode } from 'react';
import { io, Socket } from 'socket.io-client';
import { useAuthStore } from '../stores/authStore';
import { socketService } from '../services/api';

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
    console.log('🔌 [SocketContext] Effect running, isAuthenticated:', isAuthenticated);

    if (!isAuthenticated) {
      console.log('🔌 [SocketContext] Not authenticated, skipping socket setup');
      if (socketRef.current) {
        console.log('🔌 [SocketContext] User logged out, disconnecting socket');
        socketRef.current.disconnect();
        socketRef.current = null;
        setSocket(null);
      }
      setConnectionStatus('disconnected');
      return;
    }

    const token = localStorage.getItem('access_token');
    if (!token) {
      console.log('🔌 [SocketContext] No token found, skipping socket setup');
      return;
    }

    // Initialize socket connection
    if (!socketRef.current) {
      console.log('🔌 [SocketContext] Creating new socket connection');
      const newSocket = io('/', {
        path: '/socket.io/',
        transports: ['websocket', 'polling'],
        auth: { token },
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        timeout: 20000,
      });

      socketRef.current = newSocket;
      setSocket(newSocket);

      newSocket.on('connect', () => {
        console.log('🔌 [SocketContext] Connected, socket.id:', newSocket.id);
        setConnectionStatus('connected');
        newSocket.emit('authenticate', { token });
      });

      newSocket.on('authenticated', (data) => {
        console.log('🔌 [SocketContext] Authenticated:', data);
      });

      newSocket.on('disconnect', (reason) => {
        console.log('🔌 [SocketContext] Disconnected, reason:', reason);
        setConnectionStatus('disconnected');

        // If transport closed (backend restarted), reconnect gracefully
        if (reason === 'transport close' || reason === 'transport error') {
          console.log('🔌 [SocketContext] Backend likely restarted, will auto-reconnect');
          // Socket.IO will auto-reconnect due to reconnection: true
        } else if (reason === 'io server disconnect') {
          console.log('🔌 [SocketContext] Server disconnected us, manually reconnecting...');
          newSocket.connect();
        }
      });

      newSocket.on('reconnect_attempt', (attemptNumber) => {
        console.log('🔌 [SocketContext] Reconnection attempt #', attemptNumber);
        setConnectionStatus('reconnecting');
      });

      newSocket.on('reconnect', (attemptNumber) => {
        console.log('🔌 [SocketContext] Reconnected after', attemptNumber, 'attempts');
        setConnectionStatus('connected');
        const currentToken = localStorage.getItem('access_token');
        if (currentToken) {
          newSocket.emit('authenticate', { token: currentToken });
        }
      });

      newSocket.on('reconnect_error', (error) => {
        console.error('🔌 [SocketContext] Reconnection error:', error);
      });

      newSocket.on('reconnect_failed', () => {
        console.error('🔌 [SocketContext] Reconnection failed after all attempts');
        setConnectionStatus('disconnected');
      });

      newSocket.on('error', (error) => {
        console.error('🔌 [SocketContext] Error:', error);
      });

      console.log('🔌 [SocketContext] Socket created and listeners registered');
    } else if (!socketRef.current.connected) {
      console.log('🔌 [SocketContext] Socket exists but not connected, reconnecting...');
      socketRef.current.connect();
    }

    return () => {
      console.log('🔌 [SocketContext] Cleanup (socket persists)');
    };
  }, [isAuthenticated]);

  // Sync socket to socketService for legacy component support
  useEffect(() => {
    socketService.setSocket(socket);
    console.log('🔌 [SocketContext] Synced socket to socketService:', !!socket);
  }, [socket]);

  return (
    <SocketContext.Provider value={{ socket, connectionStatus }}>
      {children}
    </SocketContext.Provider>
  );
}
