export interface User {
  id: string;
  email: string;
  is_active: boolean;
  created_at: string;
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