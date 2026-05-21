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
  role?: string; // 'agent' | 'supervisor' | 'admin'
  is_active: boolean;
  created_at: string;
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
}

export interface Call {
  id: string;
  user_id: string;
  signalwire_call_sid: string;
  destination: string;
  destination_type: 'phone' | 'sip';
  status: string;
  transcription_active: boolean;
  recording_url?: string;
  summary?: string;
  duration?: number;
  created_at: string;
  answered_at?: string;
  ended_at?: string;
  full_transcript?: string;
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