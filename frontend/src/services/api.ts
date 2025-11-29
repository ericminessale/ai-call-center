import axios from 'axios';
import io, { Socket } from 'socket.io-client';
import { AuthResponse, Call, CallsListResponse, Transcription } from '../types';

// CRITICAL: Set axios defaults to prevent it from using window.location.origin
// DO NOT set a default baseURL - let axios handle relative URLs naturally
axios.defaults.headers.common['Content-Type'] = 'application/json';

// Create axios instance WITHOUT any baseURL - nginx will route /api/* to backend
const api = axios.create({
  // Let nginx handle routing - use relative URLs
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }

  // Debug: Log complete config
  console.log('Axios Request Config:', {
    url: config.url,
    baseURL: config.baseURL,
    method: config.method,
  });

  // Force remove any baseURL that might have been set
  if (config.baseURL) {
    console.warn('Removing baseURL:', config.baseURL);
    delete config.baseURL;
  }

  return config;
});

// Add interceptor to handle 401 errors
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.status === 401 && error.config && !error.config._retry) {
      error.config._retry = true;
      const refreshToken = localStorage.getItem('refresh_token');

      if (refreshToken) {
        try {
          const response = await api.post(`/api/auth/refresh`, {
            refresh_token: refreshToken,
          });

          const { access_token } = response.data;
          localStorage.setItem('access_token', access_token);

          error.config.headers.Authorization = `Bearer ${access_token}`;
          return api(error.config);
        } catch (refreshError) {
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          window.location.href = '/login';
        }
      }
    }
    return Promise.reject(error);
  }
);

export const authApi = {
  login: (email: string, password: string) =>
    api.post<AuthResponse>('/api/auth/login', { email, password }),

  register: (email: string, password: string) =>
    api.post<AuthResponse>('/api/auth/register', { email, password }),

  refresh: (refresh_token: string) =>
    api.post<AuthResponse>('/api/auth/refresh', { refresh_token }),

  me: () =>
    api.get<{ user: any }>('/api/auth/me'),

  logout: () =>
    api.post('/api/auth/logout'),
};

export const callsApi = {
  initiate: (destination: string, destination_type: 'phone' | 'sip', auto_transcribe: boolean = false) =>
    api.post<{ success: boolean; call_id: string; call_sid: string; destination: string; status: string }>(
      '/api/calls/initiate',
      { destination, destination_type, auto_transcribe }
    ),

  list: (page: number = 1, per_page: number = 10, search?: string) => {
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: per_page.toString(),
    });
    if (search?.trim()) {
      params.append('search', search.trim());
    }
    return api.get<CallsListResponse>(`/api/calls?${params}`);
  },

  get: (call_sid: string) =>
    api.get<{ call: Call; transcriptions: Transcription[] }>(`/api/calls/${call_sid}`),

  end: (call_sid: string) =>
    api.post<{ success: boolean; call_sid: string; message: string }>(`/api/calls/${call_sid}/end`),

  updateTranscription: (call_sid: string, action: 'start' | 'stop' | 'summarize') =>
    api.put<{ success: boolean; call_sid: string; action: string; message: string }>(
      `/api/calls/${call_sid}/transcription`,
      { action }
    ),

  getTranscript: (call_sid: string) =>
    api.get<{ call_sid: string; transcript: string; summary?: any }>(
      `/api/calls/${call_sid}/transcript`
    ),
};

export const transcriptionApi = {
  control: (call_sid: string, action: 'start' | 'stop' | 'summarize', prompt?: string) =>
    api.put<{ success: boolean; call_sid: string; action: string; message: string }>(
      `/api/calls/${call_sid}/transcription`,
      { action, prompt }
    ),
};

// WebSocket service
class SocketService {
  private socket: Socket | null = null;

  connect() {
    if (!this.socket) {
      // Use relative URL for WebSocket connection
      this.socket = io('/', {
        path: '/socket.io/',
        transports: ['websocket', 'polling'],
      });

      this.socket.on('connect', () => {
        console.log('WebSocket connected');

        // Send authentication token
        const token = localStorage.getItem('access_token');
        if (token) {
          this.socket?.emit('authenticate', { token });
        }
      });

      this.socket.on('disconnect', () => {
        console.log('WebSocket disconnected');
      });
    }
    return this.socket;
  }

  disconnect() {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
    }
  }

  getSocket(): Socket {
    if (!this.socket) {
      return this.connect();
    }
    return this.socket;
  }
}

export const socketService = new SocketService();

export default api;