import axios from 'axios';
import { Socket } from 'socket.io-client';
import { AuthResponse, Call, DemoWorkspace, Transcription } from '../types';
import { Contact, ContactMinimal, ContactsListResponse, InteractionsListResponse } from '../types/callcenter';

// CRITICAL: Set axios defaults to prevent it from using window.location.origin
// DO NOT set a default baseURL - let axios handle relative URLs naturally
axios.defaults.headers.common['Content-Type'] = 'application/json';

// Create axios instance WITHOUT any baseURL - nginx will route /api/* to backend
const api = axios.create({
  // Let nginx handle routing - use relative URLs
  headers: {
    'Content-Type': 'application/json',
  },
  // Required so the demo session cookie (HttpOnly, set by /api/demo/start)
  // round-trips on subsequent requests like /heartbeat. Same-origin so
  // CORS isn't a concern; this is just the cookie include flag.
  withCredentials: true,
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
    // Hosted-demo soft-blocks return 403 with code 'demo_blocked'.
    // Self-scope refusals (acting on a call that isn't yours in the demo)
    // return 403 with code 'demo_scope' — their message explains how to
    // unlock via phone verification, so prefer the server text. Content-
    // moderation rejections return 422 with code 'moderation_blocked'.
    // All surface a single user-visible toast and re-reject so callers
    // can short-circuit cleanly (the request was intentionally refused,
    // not a transient failure to retry).
    const code = error.response?.data?.code;
    if (
      (error.response?.status === 403 && (code === 'demo_blocked' || code === 'demo_scope')) ||
      (error.response?.status === 422 && code === 'moderation_blocked')
    ) {
      // Lazy import to avoid circular dep on the toast lib at module
      // load. Only fires for users actually in demo mode.
      try {
        const { default: toast } = await import('react-hot-toast');
        const fallback =
          code === 'moderation_blocked'
            ? 'Your input was flagged. Please rephrase.'
            : 'That action is not available in demo mode.';
        const message =
          code === 'demo_scope'
            ? [error.response.data.error, error.response.data.detail].filter(Boolean).join(' ')
            : error.response.data.error;
        toast.error(message || fallback);
      } catch {
        // Toast lib unavailable — error still bubbles via the reject below.
      }
      return Promise.reject(error);
    }

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

  updateMyLanguages: (languages: string[]) =>
    api.put<{ user: any }>('/api/auth/me/languages', { languages }),
};

// Runtime config — public endpoint, fetched on app boot to decide
// whether to render the production login or the hosted-demo landing.
export interface DemoPhoneNumber {
  label: string;
  number: string;
}

import type { Branding } from '../lib/branding';

export interface RuntimeConfig {
  demo_mode: boolean;
  demo_phone_numbers: DemoPhoneNumber[];
  // Workspace lifetime in days (hosted demo only) — feeds landing/banner
  // copy so it tracks the operator's WORKSPACE_TTL_DAYS.
  workspace_ttl_days?: number | null;
  // White-label branding (IMP-02); null/absent means stock SignalWire.
  branding?: Branding | null;
}

export const runtimeApi = {
  get: () => api.get<RuntimeConfig>('/api/config/runtime'),
};

// Demo session — only succeeds when DEMO_MODE=true on the server.
// Returns the same shape as authApi.login (JWT + user) so the frontend
// can hand the response straight to the existing auth handlers.
//
// /start carries a session cookie set by the backend (HttpOnly,
// SameSite=Lax). withCredentials must be true on the axios instance
// for the cookie to roundtrip — verified in the api client config.
export const demoApi = {
  start: () => api.post<AuthResponse>('/api/demo/start'),
  heartbeat: () =>
    api.post<{ ok: boolean; seat_held: boolean; workspace: DemoWorkspace | null }>(
      '/api/demo/heartbeat'
    ),
  end: () => api.post<{ ok: boolean; released: boolean }>('/api/demo/end'),
  status: () =>
    api.get<{ leased: boolean; persona: any | null; workspace: DemoWorkspace | null }>(
      '/api/demo/status'
    ),
  // Phone verification (pairing-code flow).
  pairingCode: () =>
    api.post<{ code: string }>('/api/demo/verify/pairing-code'),
  verifyStatus: () =>
    api.get<{ verified: boolean; code: string | null; masked_number: string | null }>(
      '/api/demo/verify/status'
    ),
  // "Have the AI call me" — outbound AI call to the visitor's verified number.
  // agent_type must be a known outbound agent id (see AI_AGENTS in ai_control).
  callMe: (agent_type: string = 'outbound-sales') =>
    api.post<{ success: boolean }>('/api/ai/outbound-call', {
      // Backend derives the destination from the persona's verified number;
      // it ignores any client-supplied number in demo mode (own-number gate).
      phone: 'verified',
      agent_type,
    }),
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

  // Cost transparency (IMP-01) — estimates at published list rates
  costRates: () =>
    api.get<{ rates: Record<string, number>; disclaimer: string }>('/api/calls/cost-rates'),
  costSummary: () =>
    api.get('/api/calls/cost-summary'),
  getCost: (call_sid: string) =>
    api.get(`/api/calls/${call_sid}/cost`),

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

  // Wrap-up (Tier 2a) — disposition codes + agent notes for ended calls.
  listDispositions: () =>
    api.get<{
      dispositions: Array<{ code: string; label: string; description: string }>;
    }>('/api/calls/dispositions'),
  saveWrapUp: (
    call_id: number | string,
    payload: { disposition_code?: string | null; agent_notes?: string | null }
  ) =>
    api.put<{
      success: boolean;
      call: {
        id: number;
        disposition_code: string | null;
        agent_notes: string | null;
        wrapped_up_at: string | null;
        wrap_up_source: 'ai' | 'agent' | null;
      };
    }>(`/api/calls/${call_id}/wrap-up`, payload),
};

// =============================================================================
// Callbacks API (Tier 2r)
// =============================================================================

export type CallbackStatus = 'pending' | 'claimed' | 'completed' | 'expired';
export type CallbackOutcome =
  | 'success'
  | 'no-answer'
  | 'voicemail'
  | 'declined'
  | 'wrong-number'
  | 'expired';

export interface Callback {
  id: number;
  callId: number | null;
  contactId: number | null;
  queueId: string | null;
  phoneNumber: string;
  callerName: string | null;
  reason: string | null;
  aiContext: Record<string, unknown>;
  requestedAt: string;
  expiresAt: string;
  claimedByAgentId: number | null;
  claimedAt: string | null;
  completedAt: string | null;
  attempts: number;
  outcome: CallbackOutcome | null;
  notes: string | null;
  status: CallbackStatus;
  isExpired: boolean;
  waitMinutes: number | null;
  contact?: ContactMinimal;
}

export const callbacksApi = {
  list: (params?: { queue_id?: string; status?: CallbackStatus | 'all'; mine?: boolean; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.queue_id) qs.set('queue_id', params.queue_id);
    if (params?.status) qs.set('status', params.status);
    if (params?.mine) qs.set('mine', 'true');
    if (params?.limit) qs.set('limit', String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return api.get<{ callbacks: Callback[]; total: number }>(`/api/callbacks${suffix}`);
  },

  pendingCount: (queue_id?: string) => {
    const suffix = queue_id ? `?queue_id=${encodeURIComponent(queue_id)}` : '';
    return api.get<{ pending: number }>(`/api/callbacks/pending-count${suffix}`);
  },

  get: (id: number) => api.get<{ callback: Callback }>(`/api/callbacks/${id}`),

  forContact: (contactId: number) =>
    api.get<{ callback: Callback | null }>(`/api/callbacks/for-contact/${contactId}`),

  create: (payload: {
    phone_number: string;
    call_id?: number | string;
    contact_id?: number;
    queue_id?: string;
    caller_name?: string;
    reason?: string;
    ai_context?: Record<string, unknown>;
    expiry_hours?: number;
  }) => api.post<{ callback: Callback }>('/api/callbacks', payload),

  claim: (id: number) => api.put<{ callback: Callback }>(`/api/callbacks/${id}/claim`, {}),
  release: (id: number) => api.put<{ callback: Callback }>(`/api/callbacks/${id}/release`, {}),
  recordOutcome: (
    id: number,
    payload: { outcome: CallbackOutcome; notes?: string; retry?: boolean }
  ) =>
    api.put<{ callback: Callback; retry?: Callback }>(`/api/callbacks/${id}/outcome`, payload),

  dial: (id: number) =>
    api.post<{ callback: Callback; call_id: string; outbound_call_db_id: number }>(
      `/api/callbacks/${id}/dial`
    ),
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

// Platform operator's workspace roster row (Phase 5 operator view).
export interface WorkspaceRosterRow {
  id: string;
  name: string;
  status: string;
  is_template: boolean;
  created_at: string | null;
  last_active_at: string | null;
  expires_at: string | null;
  verified_number: string | null; // masked
  connected_clients: number;
  users: number;
  queues: number;
  calls: number;
  contacts: number;
}

export interface DemoStats {
  demo_mode: boolean;
  pool_size: number;
  provisioned: number;
  active_leases: number;
  workspaces: {
    active: number;
    verified: number;
    verified_pct: number;
    created_by_day: { date: string; count: number }[];
    reaped_by_day: { date: string; count: number }[];
  };
  inbound_rejected: {
    total: number;
    by_day: { date: string; count: number }[];
  };
}

// Admin API
export const adminApi = {
  // Agent routing config
  getAgentConfig: () =>
    api.get('/api/admin/agent-config'),

  // Platform operator view (Phase 5) — platform admins only (403
  // `platform_only` for workspace-bound admins).
  listWorkspaces: () =>
    api.get<{ workspaces: WorkspaceRosterRow[]; total: number }>('/api/admin/workspaces'),
  reapWorkspace: (publicId: string) =>
    api.post<{ ok: boolean; summary: Record<string, unknown> }>(
      `/api/admin/workspaces/${publicId}/reap`
    ),
  demoStats: () =>
    api.get<DemoStats>('/api/admin/demo/stats'),
  updateAgentConfig: (config: Record<string, string>) =>
    api.put('/api/admin/agent-config', config),

  // White-label branding (IMP-02) — applies live via runtime-config refetch
  getBranding: () =>
    api.get('/api/admin/branding'),
  updateBranding: (branding: Record<string, string>) =>
    api.put('/api/admin/branding', branding),

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
  updateUserLanguages: (id: number, languages: string[]) =>
    api.put(`/api/admin/users/${id}/languages`, { languages }),
  updateUserPermissions: (id: number, permissions: Record<string, boolean>) =>
    api.put(`/api/admin/users/${id}/permissions`, { permissions }),
  updateUserKbFactbookMode: (id: number, kb_factbook_mode: 'off' | 'manual' | 'auto') =>
    api.put(`/api/admin/users/${id}/kb-factbook-mode`, { kb_factbook_mode }),
  // Admin-set: only the prompt-tone preset is set here. Per-call mode lives
  // in the LiveCallTab Coach panel as an in-call agent toggle, gated by the
  // `can_use_coach` permission flag.
  updateUserCoachIntensity: (
    id: number,
    coach_intensity: 'terse' | 'standard' | 'verbose',
  ) =>
    api.put(`/api/admin/users/${id}/coach-settings`, { coach_intensity }),
  deleteUser: (id: number) =>
    api.delete(`/api/admin/users/${id}`),
  // Recovery hammer for stuck Call Fabric registrations (Hagrid's
  // mWebRTCEndpoints leaks). Deletes the SignalWire subscriber + clears
  // local credentials so the next /api/fabric/token call mints a fresh
  // one. See CALL_TRANSPORT.md.
  resetUserSubscriber: (id: number) =>
    api.post<{
      success: boolean;
      message: string;
      deleted_subscriber_id?: string | null;
      new_subscriber_id?: string | null;
      user?: Record<string, unknown>;
      sw_warning?: string;
      recreate_error?: string;
    }>(
      `/api/admin/users/${id}/reset-subscriber`,
    ),

  // Phone number management
  getPhoneNumbers: () =>
    api.get('/api/admin/phone-numbers'),
  updatePhoneNumber: (
    sid: string,
    action: 'assign' | 'unassign',
    config?: { target_mode?: 'ai_triage' | 'ai_specialist' | 'human_direct'; target_queue_slug?: string | null },
  ) =>
    api.post(`/api/admin/phone-numbers/${sid}`, { action, ...(config || {}) }),
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

  // Webhook event log — every signed callback we accept is recorded for
  // post-hoc debugging (Tier 2i). UI paginates; backend defaults page=1, per_page=50.
  listWebhookEvents: (params?: {
    event_type?: string;
    call_id?: number;
    processed?: boolean;
    page?: number;
    per_page?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.event_type) qs.set('event_type', params.event_type);
    if (params?.call_id) qs.set('call_id', String(params.call_id));
    if (typeof params?.processed === 'boolean') qs.set('processed', String(params.processed));
    if (params?.page) qs.set('page', String(params.page));
    if (params?.per_page) qs.set('per_page', String(params.per_page));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return api.get<{
      events: Array<{
        id: number;
        call_id: number | null;
        event_type: string;
        payload: unknown;
        processed: boolean;
        error_message: string | null;
        created_at: string | null;
      }>;
      page: number;
      per_page: number;
      total: number;
      has_more: boolean;
    }>(`/api/admin/webhook-events${suffix}`);
  },
  listWebhookEventTypes: () =>
    api.get<{ event_types: string[] }>('/api/admin/webhook-events/event-types'),

  // MCP Gateway management — customer-configurable external tool integrations
  // (Salesforce, Zendesk, custom MCP servers, etc.) per agent.
  listMcpGateways: () =>
    api.get('/api/admin/mcp-gateways'),
  createMcpGateway: (data: McpGatewayInput) =>
    api.post('/api/admin/mcp-gateways', data),
  updateMcpGateway: (id: number, data: McpGatewayInput) =>
    api.put(`/api/admin/mcp-gateways/${id}`, data),
  deleteMcpGateway: (id: number) =>
    api.delete(`/api/admin/mcp-gateways/${id}`),
  testMcpGateway: (id: number) =>
    api.post<{ ok: boolean; services?: McpGatewayService[]; error?: string }>(
      `/api/admin/mcp-gateways/${id}/test`
    ),
};

export type McpGatewayAuthType = 'none' | 'basic' | 'bearer';

export interface McpGatewayInput {
  name: string;
  description?: string;
  gateway_url: string;
  auth_type: McpGatewayAuthType;
  auth_user?: string;
  auth_password?: string;
  auth_token?: string;
  services_filter?: Array<string | { name: string; tools?: string | string[] }>;
  bound_agent_ids: string[];
  enabled?: boolean;
}

export interface McpGateway {
  id: number;
  name: string;
  description: string | null;
  gateway_url: string;
  auth_type: McpGatewayAuthType;
  auth_user: string | null;
  has_auth_password: boolean;
  has_auth_token: boolean;
  services_filter: Array<string | { name: string; tools?: string | string[] }>;
  bound_agent_ids: string[];
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface McpGatewayService {
  name: string;
  description?: string | null;
  enabled?: boolean;
  tools: string[];
}

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
  getRecordingStatus: (callId: number | string) =>
    api.get<{ active: boolean; control_id: string | null; recording_url: string | null }>(`/api/call-control/${callId}/record/status`),

  // DTMF
  sendDtmf: (callId: number | string, digits: string) =>
    api.post<{ success: boolean; call_id: number; digits: string }>(`/api/call-control/${callId}/dtmf`, { digits }),

  // Monitoring (supervisor/admin only)
  startMonitor: (callId: number | string) =>
    api.post<{ success: boolean; monitor_type: 'tap' | 'conference'; dial_address?: string; token?: string; control_id?: string; conference_name?: string }>(`/api/call-control/${callId}/monitor/start`),
  stopMonitor: (callId: number | string) =>
    api.post<{ success: boolean; monitor_type: string }>(`/api/call-control/${callId}/monitor/stop`),

  // Observer whisper/barge (supervisor-initiated, permission-gated). Both
  // return a dial_address the browser dials via Call Fabric
  // (startObserverCall); hanging up that call is the stop action.
  observeWhisper: (callId: number | string) =>
    api.post<{ success: boolean; mode: 'whisper'; token: string; dial_address: string; conference_name: string }>(`/api/call-control/${callId}/observe/whisper`),
  observeBarge: (callId: number | string) =>
    api.post<{ success: boolean; mode: 'barge'; token: string; dial_address: string; conference_name: string }>(`/api/call-control/${callId}/observe/barge`),

  // Return-to-Queue (Tier 2p) — drop the agent off the call, send the caller
  // back to queue routing with the original AI context preserved. Reason is
  // mandatory + must be one of the codes the backend recognises. Soft cap at
  // 2 returns on a single call — the third attempt returns 409 must_escalate.
  returnToQueue: (
    callId: number | string,
    body: { reason: string; target_queue_slug?: string; note?: string },
  ) =>
    api.post<{
      success: boolean;
      call_id: number;
      status: string;
      queue_id: string;
      return_count: number;
      frontend_action: 'sdk_hangup';
    }>(`/api/call-control/${callId}/return-to-queue`, body),

  // Multi-agent conferencing
  requestBackup: (callId: number | string, queueId?: string) =>
    api.post<{ success: boolean; selected_agent_id: number; selected_agent_name: string; leg_id: number }>(`/api/call-control/${callId}/request-backup`, { queue_id: queueId }),
  escalate: (callId: number | string, whisper?: boolean) =>
    api.post<{ success: boolean; supervisor_id: number; supervisor_name: string; leg_id: number; whisper_mode: boolean }>(`/api/call-control/${callId}/escalate`, { whisper }),

  // Live translate (bidirectional STT + translate + TTS on the customer leg)
  startTranslate: (callId: number | string, fromLang: string, toLang: string) =>
    api.post<{ success: boolean; call_id: number; action: string; from_lang: string; to_lang: string }>(
      `/api/call-control/${callId}/translate/start`,
      { from_lang: fromLang, to_lang: toLang }
    ),
  stopTranslate: (callId: number | string) =>
    api.post<{ success: boolean; call_id: number }>(`/api/call-control/${callId}/translate/stop`),
  getTranslateStatus: (callId: number | string) =>
    api.get<{ active: boolean; from_lang: string | null; to_lang: string | null; caller_language: string | null; needs_translation: boolean }>(
      `/api/call-control/${callId}/translate/status`
    ),
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
  // SLA wallboard aggregate (IMP-18)
  getWallboard: () =>
    api.get('/api/queues/wallboard'),
  // Per-agent scorecards (supervisor/admin only)
  getAgentScorecards: (periodHours?: number) =>
    api.get<{ period_hours: number; agents: AgentScorecard[] }>(
      '/api/queues/agents/scorecards',
      { params: periodHours ? { period_hours: periodHours } : undefined },
    ),
};

// Row shape from GET /api/queues/agents/scorecards
export interface AgentScorecard {
  user_id: number;
  name: string;
  email: string;
  role: string;
  status: string;
  calls_handled: number;
  average_handle_time: number;
  total_talk_time: number;
  average_sentiment: number | null;
  returned_to_queue: number;
}

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