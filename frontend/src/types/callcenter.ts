export type AgentStatus = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

// Contact types
export type AccountTier = 'prospect' | 'free' | 'pro' | 'enterprise';
export type AccountStatus = 'active' | 'churned' | 'prospect';

export interface Contact {
  id: number;
  firstName?: string;
  lastName?: string;
  displayName: string;
  phone: string;
  email?: string;
  avatarUrl?: string;
  company?: string;
  jobTitle?: string;
  accountTier: AccountTier;
  accountStatus: AccountStatus;
  externalId?: string;
  isVip: boolean;
  isBlocked: boolean;
  tags: string[];
  notes?: string;
  customFields: Record<string, any>;
  totalCalls: number;
  lastInteractionAt?: string;
  averageSentiment?: number;
  /** R4: rolling caller-memory digest — newest-first terminal calls,
   *  {ended_at, reason, summary, disposition, handler, ai_agent}. */
  interactionDigest?: Array<{
    call_id?: number;
    ended_at?: string;
    channel?: string;
    direction?: string;
    handler?: string;
    ai_agent?: string;
    reason?: string;
    disposition?: string;
    summary?: string;
  }>;
  createdAt: string;
  updatedAt: string;
}

export interface ContactMinimal {
  id: number;
  displayName: string;
  phone: string;
  company?: string;
  accountTier: AccountTier;
  isVip: boolean;
  totalCalls: number;
  lastInteractionAt?: string;
  activeCall?: Interaction;
}

export interface CallLeg {
  id: number;
  callId: number;
  userId?: number;
  legType: 'ai_agent' | 'human_agent' | 'transfer';
  legNumber: number;
  aiAgentName?: string;
  userName?: string;
  status: 'connecting' | 'active' | 'completed';
  startedAt: string;
  endedAt?: string;
  duration?: number;
  transitionReason?: string;
  summary?: string;
  // Conference tracking
  conferenceId?: number;
  conferenceName?: string;
}

// Conference types for conference-based routing
export type ConferenceType = 'agent' | 'ai' | 'hold' | 'interaction';
export type ParticipantType = 'customer' | 'agent' | 'ai' | 'supervisor';
export type ParticipantStatus = 'joining' | 'active' | 'left' | 'muted';

export interface Conference {
  id: number;
  conferenceName: string;
  conferenceType: ConferenceType;
  ownerUserId?: number;
  ownerAiAgent?: string;
  queueId?: string;
  status: 'active' | 'ended';
  createdAt: string;
  endedAt?: string;
  participants?: ConferenceParticipant[];
}

export interface ConferenceParticipant {
  id: number;
  conferenceId: number;
  callId?: number;
  participantType: ParticipantType;
  participantId: string;
  callSid?: string;
  direction?: 'inbound' | 'outbound';  // For reporting/debugging
  status: ParticipantStatus;
  joinedAt: string;
  leftAt?: string;
  duration?: number;
  isMuted: boolean;
  isDeaf: boolean;
}

export interface Interaction {
  id: number;
  contactId?: number;
  userId: number;
  signalwireCallSid: string;
  fromNumber?: string;
  destination: string;
  destinationType: string;
  direction: 'inbound' | 'outbound';
  handlerType: 'human' | 'ai';
  aiAgentName?: string;
  status: string;
  transcriptionActive: boolean;
  recordingUrl?: string;
  summary?: string;
  duration?: number;
  sentimentScore?: number;
  aiContext: Record<string, any>;
  createdAt: string;
  answeredAt?: string;
  endedAt?: string;
  contact?: ContactMinimal;
  legs?: CallLeg[];  // Call legs for tracking handler transitions
  // Wrap-up (Tier 2a) — set by the human agent post-call.
  dispositionCode?: string | null;
  agentNotes?: string | null;
  wrappedUpAt?: string | null;
  // Provenance of the wrap-up values: 'ai' when auto-filled from the post-prompt
  // report, 'agent' once a human edits. Drives the "Captured by AI" badge.
  wrapUpSource?: 'ai' | 'agent' | null;
  // Technical ending classification (how the call ended) — deterministic,
  // distinct from dispositionCode (business outcome). See Call.compute_end_reason.
  endReason?: string | null;
}

export interface ContactsListResponse {
  contacts: ContactMinimal[];
  total: number;
  page: number;
  pages: number;
  hasNext: boolean;
  hasPrev: boolean;
}

export interface InteractionsListResponse {
  interactions: Interaction[];
  total: number;
  page: number;
  pages: number;
}

export type QueueSeverity = 'normal' | 'warning' | 'critical';
export type QueueTrend = 'increasing' | 'decreasing' | 'stable';
export type CallPriority = 'low' | 'medium' | 'high' | 'urgent';
export type Sentiment = 'very_negative' | 'negative' | 'neutral' | 'positive' | 'very_positive';

export interface Agent {
  id: string;
  name: string;
  email: string;
  status: AgentStatus;
  avatar?: string;
  skills?: string[];
  currentCall?: string;
  queues: string[];
}

export interface Queue {
  id: string;
  name: string;
  waiting: number;
  avgWait: number; // in seconds
  longest: number; // in seconds
  severity: QueueSeverity;
  trend: QueueTrend;
  slaCompliance: number; // percentage
  waitingCalls: QueuedCall[];
}

export interface QueuedCall {
  id: string;
  customerName?: string;
  phoneNumber: string;
  priority: CallPriority;
  waitTime: number; // in seconds
  isVip?: boolean;
  returnCustomer?: boolean;
  previousCalls?: number;
  aiSummary?: string;
  sentiment?: Sentiment;
  queueId: string;
}

export interface Call {
  id: number | string;
  customerName?: string;
  phoneNumber?: string;
  from_number?: string;  // Alias for phoneNumber (backend uses snake_case)
  startTime?: string | Date;  // ISO string or Date object
  created_at?: string;  // Backend timestamp
  duration?: number;
  status: 'waiting' | 'assigned' | 'connecting' | 'ringing' | 'active' | 'ai_active' | 'on_hold' | 'ended' | 'completed' | 'failed';
  direction?: 'inbound' | 'outbound';  // Populated by mapCall from backend
  isOnHold?: boolean;
  queueId?: string;
  queue_id?: string;  // Backend snake_case
  priority?: CallPriority;
  is_urgent?: boolean;  // For priority calls (timeout exceeded)
  queue_status?: 'waiting' | 'assigned' | 'urgent' | 'active';  // Computed queue status with urgency
  transcription?: TranscriptionMessage[];
  recordingUrl?: string;
  transferHistory?: Transfer[];
  assignedTo?: string;  // Agent ID
  sentiment?: number;  // -1 to 1
  aiSummary?: string;
  ai_summary?: string;  // Backend snake_case
  transferCount?: number;

  // Handler information
  handler_type?: 'human' | 'ai';
  ai_agent_name?: string;

  // Contact linkage
  contact_id?: number;
  contact?: ContactMinimal;

  // SignalWire identifiers
  signalwire_call_sid?: string;
  call_sid?: string;

  // Queue tracking
  assigned_agent_id?: number;  // Agent assigned to handle this call
  assigned_at?: string;  // When agent was notified
  conference_name?: string;  // Interaction conference name (null in bridge mode)
  wait_time_seconds?: number;  // How long caller has been waiting

  // Call transport — 'conference' (caller in interaction conf) or 'bridge'
  // (two-leg direct dial). See CALL_TRANSPORT.md.
  transport?: 'conference' | 'bridge';
  // Per-call capability set shipped from backend (call_transport.capabilities).
  // UI button visibility gates on membership in this list — never on transport
  // directly. Keep in sync with backend/app/services/call_transport/base.py.
  capabilities?: string[];
}

// Mirrors backend/app/services/call_transport/base.py Capability enum. Keep
// in sync; the source of truth is the backend.
export type CallCapability =
  | 'hold'
  | 'unhold'
  | 'send_dtmf_caller'
  | 'send_dtmf_agent'
  | 'record_start'
  | 'record_stop'
  | 'monitor_listen'
  | 'whisper'
  | 'barge'
  | 'takeover'
  | 'transfer'
  | 'live_translate'
  | 'sidecar_coach';

export interface TranscriptionMessage {
  id?: string;
  // 'caller' is an alias for 'customer'; 'system' rows are synthetic markers
  // (e.g. the AI→human handoff divider) — rendered as a seam, not speech.
  speaker: 'agent' | 'customer' | 'ai' | 'caller' | 'system';
  speakerName?: string;
  text: string;
  timestamp: string | Date;  // ISO string or Date object
  sentiment?: Sentiment;
  metadata?: Record<string, any>;
}

export interface Transfer {
  from: string;
  to: string;
  type: 'warm' | 'cold';
  timestamp: Date;
  notes?: string;
}

export interface CustomerContext {
  customerId?: string;
  customerName?: string;
  accountNumber?: string;
  email?: string;
  phone?: string;
  previousCalls?: number;
  lastCallDate?: Date;
  issueDescription?: string;
  department?: string;
  priority?: CallPriority;
  sentiment?: Sentiment;
  isVip?: boolean;
  notes?: string[];
  tags?: string[];

  // AI-specific context
  aiSummary?: string;
  aiConfidence?: number;
  aiIntent?: string;
  aiActions?: string[];
  extractedInfo?: ExtractedInfo[];
}

export interface ExtractedInfo {
  key: string;
  label: string;
  value: string;
  confidence?: number;
}

export interface AgentStats {
  callsToday: number;
  avgHandleTime: number; // in seconds
  queueDepth: number;
  longestWait: number; // in seconds
}

// Configurable Queue System types
export type RoutingStrategy = 'fifo' | 'round_robin' | 'priority' | 'skill_based';

export interface QueueConfig {
  id: number;
  slug: string;
  display_name: string;
  description: string | null;
  is_active: boolean;
  routing_strategy: RoutingStrategy;
  ai_agent_route: string | null;
  default_priority: number;
  sla_threshold_seconds: number;
  max_wait_before_ai_fallback: number;
  agent_count?: number;
  created_at?: string;
  updated_at?: string;
}

export interface QueueAgentAssignmentType {
  id: number;
  queue_id: number;
  queue_slug: string;
  queue_display_name: string;
  routing_strategy: RoutingStrategy;
  user_id: number;
  user_name: string | null;
  user_email: string;
  skill_level: number;
  is_activated: boolean;
}

export interface QueueAttemptTimeline {
  id: number;
  callId: number;
  queueId?: number | null;
  queueSlug: string;
  queueDisplayName?: string | null;
  attemptNumber: number;
  priority?: number | null;
  routingStrategy?: string | null;
  transport?: 'conference' | 'bridge' | null;
  enteredAt: string;
  serviceStartedAt: string;
  firstOfferedAt?: string | null;
  lastOfferedAt?: string | null;
  lastOfferedAgentId?: number | null;
  lastOfferedAgentName?: string | null;
  offerCount: number;
  declinedOfferCount: number;
  lastDeclinedAt?: string | null;
  lastDeclinedAgentId?: number | null;
  lastDeclinedAgentName?: string | null;
  acceptedAt?: string | null;
  acceptedAgentId?: number | null;
  acceptedAgentName?: string | null;
  exitedAt?: string | null;
  exitReason?: string | null;
  waitSeconds?: number | null;
}

export type HandlingSegmentType = 'ai' | 'human' | 'hold' | 'consultation';

export interface HandlingSegmentTimeline {
  id: number;
  callId: number;
  queueAttemptId?: number | null;
  type: HandlingSegmentType;
  agentId?: number | null;
  agentName?: string | null;
  aiAgentName?: string | null;
  transport?: 'conference' | 'bridge' | null;
  startedAt: string;
  endedAt?: string | null;
  endReason?: string | null;
  durationSeconds?: number | null;
  details: Record<string, unknown>;
}

export interface CallTimelineResponse {
  callId: number;
  signalwireCallId: string;
  transport?: 'conference' | 'bridge' | null;
  queueAttempts: QueueAttemptTimeline[];
  handlingSegments: HandlingSegmentTimeline[];
}
