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
export type ConferenceType = 'agent' | 'ai' | 'hold';
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
  status: 'waiting' | 'connecting' | 'active' | 'ai_active' | 'on_hold' | 'ended' | 'completed';
  isOnHold?: boolean;
  queueId?: string;
  queue_id?: string;  // Backend snake_case
  priority?: CallPriority;
  is_urgent?: boolean;  // For priority calls
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
}

export interface TranscriptionMessage {
  id?: string;
  speaker: 'agent' | 'customer' | 'ai' | 'caller';  // Added 'caller' alias for 'customer'
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

export interface PerformanceMetrics {
  callsToday: number;
  avgHandleTime: number; // in milliseconds
  avgHandleTimeYesterday: number;
  fcr: number; // First Call Resolution percentage
  csat: number; // Customer Satisfaction score
  perfectDays: number;
  nextMilestone: number;
  isPersonalBest: boolean;
}