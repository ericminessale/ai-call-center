import { useSocketContext } from '../contexts/SocketContext';

/**
 * Hook to access the shared WebSocket connection.
 * This uses the SocketContext which manages a single socket connection for the entire app.
 *
 * @returns The Socket.IO socket instance, or null if not connected
 */
export const useSocket = () => {
  const { socket } = useSocketContext();
  return socket;
};

/**
 * Hook to access socket connection status.
 *
 * @returns Object with socket and connectionStatus
 */
export const useSocketWithStatus = () => {
  return useSocketContext();
};
