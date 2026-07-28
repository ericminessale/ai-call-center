// Capability flags surfaced by the backend. Adding a new flag here requires
// adding it to PERMISSION_FLAGS in backend/app/models/user.py. Keep in sync.
export type PermissionFlag =
  | 'can_listen_ai_calls'
  | 'can_listen_human_calls'
  | 'can_whisper'
  | 'can_barge'
  | 'can_control_recording'
  | 'can_use_coach';

export interface User {
  id: string;
  email: string;
  name?: string;
  // 'agent' | 'supervisor' | 'admin' | 'visitor' (hosted-demo workspace owner).
  // Gate UI on the helpers in lib/roles.ts, not on string equality — 'visitor'
  // has a supervisor's reach but not admin-management powers (HIGH-3).
  role?: string;
  is_active: boolean;
  created_at: string;
  // Tenancy: null = platform-level user (hosted operator / clone-and-own
  // admin), number = workspace-bound (hosted demo visitor + colleagues).
  // Gates the platform-only Workspaces view.
  workspace_id?: number | null;
  languages?: string[]; // BCP-47 codes (e.g. ["en-US", "es-ES"])
  // Resolved flag map after role defaults merge with per-user overrides.
  // UI gates observer controls off this. Shipped on every /auth/me response.
  effective_permissions?: Partial<Record<PermissionFlag, boolean>>;
  // Explicit per-user overrides; drives the "overridden" state in the admin
  // user-config drawer. Empty = pure role defaults.
  permission_overrides?: Partial<Record<PermissionFlag, boolean>>;
  // Agent Assist — Knowledge Factbook mode. See AGENT_ASSIST.md.
  kb_factbook_mode?: 'off' | 'manual' | 'auto';
  // Agent Assist — AI Coach prompt-tone preset. Mode is picked per-call in
  // the live Coach panel (gated by `can_use_coach` permission), so it's
  // intentionally not in this profile shape.
  coach_intensity?: 'terse' | 'standard' | 'verbose';
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user: User;
  message: string;
  // Hosted demo only (/api/demo/start): the visitor's workspace, including
  // its expiry — feeds the banner's lifetime display.
  workspace?: DemoWorkspace;
}

// Hosted-demo workspace lifecycle info (id = public id).
export interface DemoWorkspace {
  id: string;
  name: string;
  status: string;
  created_at: string | null;
  expires_at: string | null;
}

export interface Call {
  id: number;
  userId: number;
  contactId?: number | null;
  signalwireCallSid: string;
  fromNumber?: string | null;
  destination: string;
  destinationType: 'phone' | 'sip';
  direction: 'inbound' | 'outbound';
  handlerType: 'human' | 'ai';
  aiAgentName?: string | null;
  status: string;
  transcriptionActive: boolean;
  recordingUrl?: string | null;
  summary?: string | null;
  duration?: number | null;
  sentimentScore?: number | null;
  createdAt: string;
  answeredAt?: string | null;
  endedAt?: string | null;
  queue_id?: string | null;
  assigned_agent_id?: number | null;
  transport?: 'conference' | 'bridge';
}

export interface Transcription {
  id: string;
  call_id: string;
  transcript?: string;
  summary?: string;
  confidence?: number;
  is_final: boolean;
  sequence_number?: number;
  language: string;
  keywords?: string[];
  sentiment?: string;
  created_at: string;
}

export interface CallsListResponse {
  calls: Call[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}
