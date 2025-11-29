export type AgentStatus = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

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
  id: string;
  customerName?: string;
  phoneNumber: string;
  startTime?: string | Date;  // ISO string or Date object
  duration?: number;
  status: 'waiting' | 'connecting' | 'active' | 'ai_active' | 'on_hold' | 'ended' | 'completed';
  isOnHold?: boolean;
  queueId?: string;
  priority?: CallPriority;
  transcription?: TranscriptionMessage[];
  recordingUrl?: string;
  transferHistory?: Transfer[];
  assignedTo?: string;  // Agent ID
  sentiment?: number;  // -1 to 1
  aiSummary?: string;
  transferCount?: number;
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