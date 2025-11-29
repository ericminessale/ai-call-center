import { io, Socket } from 'socket.io-client';

class WebSocketService {
  private socket: Socket | null = null;
  private listeners: Map<string, Set<(data: any) => void>> = new Map();

  connect(token: string) {
    // Use relative path - nginx will handle the WebSocket proxy
    const WS_URL = import.meta.env.VITE_WS_URL || window.location.origin;

    this.socket = io(WS_URL, {
      path: '/socket.io/',
      auth: {
        token,
      },
      transports: ['websocket', 'polling'],
    });

    this.socket.on('connect', () => {
      console.log('WebSocket connected');
    });

    this.socket.on('disconnect', () => {
      console.log('WebSocket disconnected');
    });

    // Re-emit all registered events
    this.listeners.forEach((callbacks, event) => {
      this.socket?.on(event, (data) => {
        callbacks.forEach(callback => callback(data));
      });
    });
  }

  disconnect() {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
    }
  }

  on(event: string, callback: (data: any) => void) {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event)?.add(callback);

    if (this.socket) {
      this.socket.on(event, callback);
    }
  }

  off(event: string, callback: (data: any) => void) {
    this.listeners.get(event)?.delete(callback);

    if (this.socket) {
      this.socket.off(event, callback);
    }
  }

  emit(event: string, data: any) {
    if (this.socket) {
      this.socket.emit(event, data);
    }
  }

  joinRoom(room: string) {
    this.emit('join', room);
  }

  leaveRoom(room: string) {
    this.emit('leave', room);
  }
}

export default new WebSocketService();