import axios from 'axios';
import io, { Socket } from 'socket.io-client';
import { AuthResponse, Call, CallsListResponse, Transcription } from '../types';
import { Contact, ContactMinimal, ContactsListResponse, Interaction, InteractionsListResponse } from '../types/callcenter';

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

  list: (params?: {
    page?: number;
    per_page?: number;
    search?: string;
    status?: string;  // Can be comma-separated for multiple statuses
    agent_id?: string;
  }) => {
    const urlParams = new URLSearchParams();
    if (params?.page) urlParams.append('page', params.page.toString());
    if (params?.per_page) urlParams.append('per_page', params.per_page.toString());
    if (params?.search?.trim()) urlParams.append('search', params.search.trim());
    // Backend expects multiple status params (e.g., ?status=active&status=ai_active)
    if (params?.status) {
      const statuses = params.status.split(',');
      statuses.forEach(s => urlParams.append('status', s.trim()));
    }
    if (params?.agent_id) urlParams.append('agent_id', params.agent_id);
    return api.get<{ calls: Call[]; total: number; page: number; pages: number }>(`/api/calls?${urlParams}`);
  },

  get: (call_sid: string) =>
    api.get<{ call: Call; transcriptions: Transcription[] }>(`/api/calls/${call_sid}`),

  end: (call_sid: string) =>
    api.post<{ success: boolean; call_sid: string; message: string }>(`/api/calls/${call_sid}/end`),

  take: (call_id: number | string) =>
    api.post<{ success: boolean; call_id: number; message: string }>(`/api/calls/${call_id}/take`),

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

export const contactsApi = {
  list: (params?: {
    search?: string;
    page?: number;
    per_page?: number;
    sort_by?: 'last_interaction' | 'name' | 'created';
    include_blocked?: boolean;
  }) => {
    const queryParams = new URLSearchParams();
    if (params?.search) queryParams.append('search', params.search);
    if (params?.page) queryParams.append('page', params.page.toString());
    if (params?.per_page) queryParams.append('per_page', params.per_page.toString());
    if (params?.sort_by) queryParams.append('sort_by', params.sort_by);
    if (params?.include_blocked) queryParams.append('include_blocked', 'true');
    return api.get<ContactsListResponse>(`/api/contacts?${queryParams}`);
  },

  get: (contactId: number) =>
    api.get<Contact>(`/api/contacts/${contactId}`),

  create: (data: Partial<Contact>) =>
    api.post<Contact>('/api/contacts', data),

  update: (contactId: number, data: Partial<Contact>) =>
    api.put<Contact>(`/api/contacts/${contactId}`, data),

  delete: (contactId: number) =>
    api.delete(`/api/contacts/${contactId}`),

  lookup: (phone: string) =>
    api.get<Contact | { contact: null; found: false }>(`/api/contacts/lookup?phone=${encodeURIComponent(phone)}`),

  lookupOrCreate: (data: { phone: string; firstName?: string; lastName?: string; displayName?: string; company?: string }) =>
    api.post<{ contact: Contact; created: boolean }>('/api/contacts/lookup-or-create', data),

  getInteractions: (contactId: number, page?: number, per_page?: number) => {
    const params = new URLSearchParams();
    if (page) params.append('page', page.toString());
    if (per_page) params.append('per_page', per_page.toString());
    return api.get<InteractionsListResponse>(`/api/contacts/${contactId}/interactions?${params}`);
  },

  getRecent: (limit?: number) =>
    api.get<{ contacts: ContactMinimal[] }>(`/api/contacts/recent${limit ? `?limit=${limit}` : ''}`),

  getActive: () =>
    api.get<{ contacts: ContactMinimal[] }>('/api/contacts/active'),
};

// WebSocket service - now uses a shared socket from SocketContext
// This is a legacy compatibility layer. Prefer using useSocketContext() in components.
class SocketService {
  private socket: Socket | null = null;

  // Set the shared socket from SocketContext
  setSocket(socket: Socket | null) {
    this.socket = socket;
  }

  // Legacy method - no longer creates its own socket
  connect(): Socket | null {
    console.warn('[socketService] connect() called - socket should be managed by SocketContext');
    return this.socket;
  }

  disconnect() {
    console.warn('[socketService] disconnect() called - socket should be managed by SocketContext');
  }

  getSocket(): Socket | null {
    if (!this.socket) {
      console.warn('[socketService] getSocket() called but no socket available. Use useSocketContext() instead.');
    }
    return this.socket;
  }
}

export const socketService = new SocketService();

export default api;