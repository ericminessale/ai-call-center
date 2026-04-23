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

  // Force remove any baseURL that might have been set
  if (config.baseURL) {
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
    api.post<{ success: boolean; call_id: number; message: string; conference_name?: string }>(`/api/calls/${call_id}/take`),

  // Update call status (called when agent joins/leaves conference)
  updateStatus: (call_id: number | string, status: string) =>
    api.put<{ success: boolean; call_id: number; status: string }>(`/api/calls/${call_id}/status`, { status }),

  updateTranscription: (call_sid: string, action: 'start' | 'stop' | 'summarize') =>
    api.put<{ success: boolean; call_sid: string; action: string; message: string }>(
      `/api/calls/${call_sid}/transcription`,
      { action }
    ),

  getTranscript: (call_sid: string) =>
    api.get<{ call_sid: string; transcript: string; summary?: any }>(
      `/api/calls/${call_sid}/transcript`
    ),

  // Get all queued calls (waiting, assigned, urgent) sorted by urgency
  getQueuedCalls: () =>
    api.get<{ calls: Call[]; total: number }>('/api/queues/all/calls'),

  // Get real-time agent stats for header bar
  getMyStats: () =>
    api.get<{ success: boolean; stats: { callsToday: number; avgHandleTime: number; queueDepth: number; longestWait: number } }>('/api/calls/my-stats'),
};

export const conferencesApi = {
  // Prepare a conference join - stores params in Redis and returns a token
  // This is called BEFORE dialing to ensure params are reliably passed
  prepareJoin: (params: {
    agent_id: number;
    conference_name: string;
    call_id?: number | string;
    type?: 'monitor' | 'backup' | 'escalation';
    context?: Record<string, any>;
    whisper_mode?: boolean;
    agent_call_sid?: string;
  }) =>
    api.post<{ token: string; dial_address: string; conference_name: string }>(
      '/api/conferences/prepare-join',
      params
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

// Admin API
export const adminApi = {
  // Agent routing config
  getAgentConfig: () =>
    api.get('/api/admin/agent-config'),
  updateAgentConfig: (config: Record<string, string>) =>
    api.put('/api/admin/agent-config', config),

  // Document collections
  getCollections: () =>
    api.get('/api/admin/collections'),
  createCollection: (data: { name: string; display_name: string; description?: string }) =>
    api.post('/api/admin/collections', data),
  updateCollection: (id: number, data: { display_name?: string; description?: string }) =>
    api.put(`/api/admin/collections/${id}`, data),
  deleteCollection: (id: number) =>
    api.delete(`/api/admin/collections/${id}`),

  // Documents
  getDocuments: (collectionId: number) =>
    api.get(`/api/admin/collections/${collectionId}/documents`),
  createDocument: (collectionId: number, data: { title: string; content: string }) =>
    api.post(`/api/admin/collections/${collectionId}/documents`, data),
  updateDocument: (id: number, data: { title?: string; content?: string }) =>
    api.put(`/api/admin/documents/${id}`, data),
  deleteDocument: (id: number) =>
    api.delete(`/api/admin/documents/${id}`),
  reindexCollection: (id: number) =>
    api.post(`/api/admin/collections/${id}/reindex`),

  // Agent-collection assignments
  getAgentAssignments: () =>
    api.get('/api/admin/agent-assignments'),
  updateAgentAssignments: (assignments: { assignments: Array<{ agent_id: string; collection_id: number }> }) =>
    api.put('/api/admin/agent-assignments', assignments),

  // User management
  listUsers: () =>
    api.get('/api/admin/users'),
  updateUserRole: (id: number, role: 'admin' | 'supervisor' | 'agent') =>
    api.put(`/api/admin/users/${id}`, { role }),
  deleteUser: (id: number) =>
    api.delete(`/api/admin/users/${id}`),

  // Phone number management
  getPhoneNumbers: () =>
    api.get('/api/admin/phone-numbers'),
  updatePhoneNumber: (sid: string, action: 'assign' | 'unassign') =>
    api.post(`/api/admin/phone-numbers/${sid}`, { action }),
  getWebhookUrl: () =>
    api.get('/api/admin/webhook-url'),

  // Queue management
  getQueues: () =>
    api.get('/api/admin/queues'),
  createQueue: (data: { slug: string; display_name: string; description?: string; routing_strategy?: string; ai_agent_route?: string; default_priority?: number; sla_threshold_seconds?: number; max_wait_before_ai_fallback?: number }) =>
    api.post('/api/admin/queues', data),
  updateQueue: (id: number, data: Record<string, unknown>) =>
    api.put(`/api/admin/queues/${id}`, data),
  deleteQueue: (id: number) =>
    api.delete(`/api/admin/queues/${id}`),
  getQueueAgents: (queueId: number) =>
    api.get(`/api/admin/queues/${queueId}/agents`),
  updateQueueAgents: (queueId: number, assignments: Array<{ user_id: number; skill_level?: number }>) =>
    api.put(`/api/admin/queues/${queueId}/agents`, { assignments }),
};

// Real-time call control
export const callControlApi = {
  // Hold/Resume
  hold: (callId: number | string) =>
    api.post<{ success: boolean; call_id: number; status: string }>(`/api/call-control/${callId}/hold`),
  unhold: (callId: number | string) =>
    api.post<{ success: boolean; call_id: number; status: string }>(`/api/call-control/${callId}/unhold`),

  // Play audio/TTS into call
  playTts: (callId: number | string, text: string, voice?: string) =>
    api.post<{ success: boolean; call_id: number }>(`/api/call-control/${callId}/play`, { type: 'tts', text, voice }),
  playAudio: (callId: number | string, url: string) =>
    api.post<{ success: boolean; call_id: number }>(`/api/call-control/${callId}/play`, { type: 'audio', url }),

  // Recording
  startRecording: (callId: number | string) =>
    api.post<{ success: boolean; call_id: number; recording: boolean; control_id?: string }>(`/api/call-control/${callId}/record/start`),
  stopRecording: (callId: number | string) =>
    api.post<{ success: boolean; call_id: number; recording: boolean }>(`/api/call-control/${callId}/record/stop`),

  // DTMF
  sendDtmf: (callId: number | string, digits: string) =>
    api.post<{ success: boolean; call_id: number; digits: string }>(`/api/call-control/${callId}/dtmf`, { digits }),

  // Monitoring (supervisor/admin only)
  startMonitor: (callId: number | string) =>
    api.post<{ success: boolean; monitor_type: 'tap' | 'conference'; dial_address?: string; token?: string; control_id?: string; conference_name?: string }>(`/api/call-control/${callId}/monitor/start`),
  stopMonitor: (callId: number | string) =>
    api.post<{ success: boolean; monitor_type: string }>(`/api/call-control/${callId}/monitor/stop`),

  // Multi-agent conferencing
  requestBackup: (callId: number | string, queueId?: string) =>
    api.post<{ success: boolean; selected_agent_id: number; selected_agent_name: string; leg_id: number }>(`/api/call-control/${callId}/request-backup`, { queue_id: queueId }),
  escalate: (callId: number | string, whisper?: boolean) =>
    api.post<{ success: boolean; supervisor_id: number; supervisor_name: string; leg_id: number; whisper_mode: boolean }>(`/api/call-control/${callId}/escalate`, { whisper }),
};

// Agent-facing queue operations
export const queueApi = {
  getMyQueues: () =>
    api.get('/api/queues/my-queues'),
  toggleQueueActivation: (assignmentId: number, isActivated: boolean) =>
    api.put(`/api/queues/my-queues/${assignmentId}/activate`, { is_activated: isActivated }),
  getAvailableQueues: () =>
    api.get('/api/queues/available'),
  selfSubscribe: (queueId: number) =>
    api.post(`/api/queues/self-subscribe/${queueId}`),
  getActiveQueueConfigs: () =>
    api.get('/api/queues/config/active'),
};

// WebSocket service - now uses a shared socket from SocketContext
// This is a legacy compatibility layer. Prefer using useSocketContext() in components.
class SocketService {
  private socket: Socket | null = null;

  // Set the shared socket from SocketContext
  setSocket(socket: Socket | null) {
    this.socket = socket;
  }

  connect(): Socket | null {
    return this.socket;
  }

  disconnect() {
    // Managed by SocketContext
  }

  getSocket(): Socket | null {
    return this.socket;
  }
}

export const socketService = new SocketService();

export default api;