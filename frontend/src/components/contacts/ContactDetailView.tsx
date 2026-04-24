import { useState, useEffect, useRef } from 'react';
import {
  Phone,
  PhoneOff,
  Bot,
  Mail,
  Edit2,
  Star,
  Clock,
  MessageSquare,
  ChevronDown,
  ChevronUp,
  Tag,
  Building2,
  User,
  Ban,
  MoreHorizontal,
  Trash2,
  PhoneOutgoing,
  PhoneIncoming,
  Mic,
  MicOff,
  FileText,
  Pause,
  Play,
  Send,
  AlertCircle,
  X,
  PhoneCall,
  Users,
  Loader2,
} from 'lucide-react';
import { Contact, Interaction, TranscriptionMessage, Call, CallLeg } from '../../types/callcenter';
import { contactsApi, callsApi, callControlApi } from '../../services/api';
import api from '../../services/api';
import { useCallFabric } from '../../hooks/useCallFabric';
import { useCallFabricContext } from '../../contexts/CallFabricContext';
import { useSocketContext } from '../../contexts/SocketContext';
import { CallTimeline } from './CallTimeline';
import { LiveCallTab, SentimentData } from './LiveCallTab';
import { CallDetailTab } from './CallDetailTab';
import CallControlPanel from './CallControlPanel';
import { ConferenceParticipants } from './ConferenceParticipants';
import { useAuthStore } from '../../stores/authStore';

interface ContactDetailViewProps {
  contact: Contact;
  onContactUpdate: (contact: Contact) => void;
  onContactDelete?: (contactId: number) => void;
  activeCallForContact?: Call; // Inbound/AI call for this contact from parent
  liveSentiment?: SentimentData | null; // Real-time sentiment from AI agent
}

/** Renders AI context summary as readable text instead of raw JSON */
export function AISummaryDisplay({ summary }: { summary: string }) {
  // Try to parse as JSON — if it looks like AI context, render it nicely
  try {
    const parsed = JSON.parse(summary);
    if (typeof parsed === 'object' && parsed !== null) {
      const { customer_name, department, reason, outcome, notes, ...rest } = parsed;
      const parts: string[] = [];
      if (reason) parts.push(reason);
      if (notes && notes !== reason) parts.push(notes);
      if (!parts.length) {
        // Fallback: render all values as a sentence
        parts.push(...Object.values(rest).filter(v => typeof v === 'string' && v !== 'unknown' && v !== 'not specified') as string[]);
      }

      return (
        <span className="inline">
          {parts.length > 0 ? (
            <span>{parts.join(' — ')}</span>
          ) : (
            <span className="text-gray-500 italic">No summary available</span>
          )}
          {(department && department !== 'unknown') && (
            <span className="ml-2 px-1.5 py-0.5 text-[10px] rounded bg-gray-600 text-gray-300">
              {department}
            </span>
          )}
          {outcome && (
            <span className={`ml-1 px-1.5 py-0.5 text-[10px] rounded ${
              outcome === 'resolved' ? 'bg-green-900/40 text-green-300' :
              outcome === 'transferred_to_human' ? 'bg-blue-900/40 text-blue-300' :
              outcome === 'abandoned' ? 'bg-red-900/40 text-red-300' :
              'bg-gray-600 text-gray-300'
            }`}>
              {outcome.replace(/_/g, ' ')}
            </span>
          )}
        </span>
      );
    }
  } catch {
    // Not JSON — render as plain text
  }
  return <span>{summary}</span>;
}

export function ContactDetailView({ contact, onContactUpdate, onContactDelete, activeCallForContact, liveSentiment }: ContactDetailViewProps) {
  const [interactions, setInteractions] = useState<Interaction[]>([]);
  const [isLoadingInteractions, setIsLoadingInteractions] = useState(false);
  const [showAllNotes, setShowAllNotes] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [activeTab, setActiveTab] = useState<'history' | 'notes' | 'details' | 'live' | 'callDetail'>('history');
  const [showMoreMenu, setShowMoreMenu] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const moreMenuRef = useRef<HTMLDivElement>(null);

  // Selected historical call for detail view
  const [selectedHistoryCall, setSelectedHistoryCall] = useState<Interaction | null>(null);

  // Get shared socket from context
  const { socket } = useSocketContext();

  // Call state
  const [transcription, setTranscription] = useState<TranscriptionMessage[]>([]);
  const [callDuration, setCallDuration] = useState(0);
  const [isAICall, setIsAICall] = useState(false);
  const [currentCallSid, setCurrentCallSid] = useState<string | null>(null);

  // Determine if there's any active call (browser call, outbound AI, OR inbound AI)
  const inboundCallSid = activeCallForContact?.signalwire_call_sid || (activeCallForContact as any)?.call_sid;
  const isInboundAICall = activeCallForContact?.status === 'ai_active' || activeCallForContact?.handler_type === 'ai';
  const effectiveCallSid = currentCallSid || inboundCallSid;

  // Call Fabric hook
  const {
    activeCall,
    isOnline,
    isInitializing,
    error: callError,
    callState,
    isMuted,
    makeCall,
    hangup,
    goOnline,
    mute,
    unmute,
    connectedCustomer,
  } = useCallFabric();

  // Call Fabric context for taking queued calls
  const { acceptCallAssignmentWithData, isClientReady, isInConference, conferenceParticipants, joinInteractionConference, makeCallToSwml } = useCallFabricContext();

  // State for taking queued calls
  const [isTakingCall, setIsTakingCall] = useState(false);

  // Call control state
  const [isOnHold, setIsOnHold] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const { user: currentUser } = useAuthStore();

  // Hydrate recording state from backend when a call becomes active, so the
  // Record button reflects reality on first render instead of always showing
  // "Record" regardless of the call's actual state.
  useEffect(() => {
    const callId = activeCallForContact?.id;
    if (!callId) {
      setIsRecording(false);
      return;
    }
    let cancelled = false;
    callControlApi
      .getRecordingStatus(callId)
      .then((res) => {
        if (!cancelled) setIsRecording(Boolean(res.data.active));
      })
      .catch(() => {
        // Ignore — endpoint may 404 for non-eligible call types. Default off.
      });
    return () => {
      cancelled = true;
    };
  }, [activeCallForContact?.id]);
  const [takeCallError, setTakeCallError] = useState<string | null>(null);

  // AI Agent form state
  const [showAIForm, setShowAIForm] = useState(false);
  const [availableAgents, setAvailableAgents] = useState<{ id: string; name: string; route: string; description: string }[]>([]);
  const [aiFormData, setAiFormData] = useState({
    agentType: 'outbound-sales',
    contactName: '',
    company: '',
    accountTier: '',
    isVip: false,
    additionalContext: '',
  });
  const [isSubmittingAI, setIsSubmittingAI] = useState(false);
  const [aiFormError, setAiFormError] = useState<string | null>(null);

  // Check if this contact has a call in the queue (waiting, assigned, queued, or urgent)
  // BUT not if the agent is already in conference (connected to the call)
  const queueStatuses = ['waiting', 'assigned', 'queued', 'urgent'];
  const isCallInQueue = activeCallForContact &&
    queueStatuses.includes(activeCallForContact.status || '') &&
    !isInConference;  // Don't show queue banner when agent is connected
  const queueStatus = activeCallForContact?.queue_status || activeCallForContact?.status;
  const isUrgent = activeCallForContact?.is_urgent || queueStatus === 'urgent';
  const waitTime = activeCallForContact?.wait_time_seconds;

  // Handle taking a queued call
  const handleTakeQueuedCall = async () => {
    if (!activeCallForContact) return;

    setIsTakingCall(true);
    setTakeCallError(null);

    try {
      // Call the take API to assign the call to this agent
      const response = await callsApi.take(activeCallForContact.id);
      const conferenceName = response.data.conference_name || activeCallForContact.conference_name;

      if (!conferenceName) {
        throw new Error('No conference name returned - call may have ended');
      }

      // Dial into the conference
      // Pass AI context from the call so it's available for display
      const aiContext = (activeCallForContact as any).aiContext || {};
      console.log('📋 [ContactDetail] Passing AI context to call:', aiContext);

      await acceptCallAssignmentWithData({
        callId: String(activeCallForContact.signalwire_call_sid || activeCallForContact.call_sid || activeCallForContact.id),
        callDbId: Number(activeCallForContact.id),
        callerNumber: activeCallForContact.from_number || activeCallForContact.phoneNumber || '',
        queueId: activeCallForContact.queue_id || activeCallForContact.queueId || '',
        conferenceName: conferenceName,
        customerInfo: {
          phone: activeCallForContact.from_number || activeCallForContact.phoneNumber || '',
          name: activeCallForContact.customerName || contact.displayName,
          contact_id: contact.id,
        },
        context: aiContext,
      });

      console.log('✅ [ContactDetail] Successfully took queued call');
    } catch (error: any) {
      console.error('❌ [ContactDetail] Failed to take call:', error);
      setTakeCallError(error?.response?.data?.error || error?.message || 'Failed to take call');
    } finally {
      setIsTakingCall(false);
    }
  };

  // Format wait time display
  const formatWaitTime = (seconds?: number) => {
    if (!seconds) return '';
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}m ${secs}s`;
  };

  // Determine if the agent is connected to a call (via conference or direct)
  // When agent is in conference, treat it as "connected" not "outbound calling"
  const isAgentConnected = isInConference && (callState === 'active' || callState === 'ringing');

  // Determine if there's a TRUE outbound call in progress (agent calling out to customer)
  // NOT including conference joins for inbound calls
  const isTrueOutboundCall = activeCallForContact?.direction === 'outbound' &&
    ['ringing', 'active', 'connecting'].includes(activeCallForContact?.status || '');

  // Show "ringing/calling" only for true outbound calls, not conference joins
  const isOutboundCallInProgress = isTrueOutboundCall ||
    (callState !== 'idle' && !isInConference);  // Only show ringing if NOT already in conference

  // Any active call: browser outbound, AI outbound, inbound conference, or inbound from parent
  const hasAnyActiveCall = !!(activeCall || currentCallSid || activeCallForContact || isOutboundCallInProgress || isInConference);

  // Get display status for calls
  const getOutboundCallStatus = () => {
    // If agent is in conference (connected to inbound call), show Connected
    if (isAgentConnected) return 'Connected';

    // First check browser callState (for browser-initiated calls)
    if (callState === 'ringing' && !activeCall) return 'Calling...';
    if (callState === 'ringing') return 'Ringing...';
    if (callState === 'active') return 'Connected';
    if (callState === 'ending') return 'Ending...';

    // Then check activeCallForContact status (for server-initiated dial-out)
    if (activeCallForContact?.direction === 'outbound') {
      const status = activeCallForContact.status;
      if (status === 'ringing' || status === 'connecting') return 'Ringing...';
      if (status === 'active') return 'Connected';
      if (status === 'ended' || status === 'failed') return 'Ended';
    }

    return 'Connected';
  };

  // Load interactions when contact changes
  useEffect(() => {
    loadInteractions();
  }, [contact.id]);

  // Track call duration
  useEffect(() => {
    if (!activeCall) {
      setCallDuration(0);
      return;
    }

    const interval = setInterval(() => {
      setCallDuration(prev => prev + 1);
    }, 1000);

    return () => clearInterval(interval);
  }, [activeCall]);

  // Listen for call end events via WebSocket (clears local call state when remote party hangs up)
  useEffect(() => {
    if (!socket || !currentCallSid) return;

    const handleCallUpdate = (data: { call: any }) => {
      const call = data.call;
      if (!call) return;
      const callSid = call.call_sid || call.signalwire_call_sid || call.signalwireCallSid;
      if (callSid === currentCallSid && ['ended', 'completed', 'failed'].includes(call.status)) {
        console.log('📞 [ContactDetail] Call ended via socket event, clearing local state');
        setCurrentCallSid(null);
        setIsAICall(false);
        loadInteractions();
      }
    };

    const handleCallEnded = (data: { call_sid?: string; callId?: number }) => {
      if (data.call_sid === currentCallSid) {
        console.log('📞 [ContactDetail] call_ended event received, clearing local state');
        setCurrentCallSid(null);
        setIsAICall(false);
        loadInteractions();
      }
    };

    socket.on('call_update', handleCallUpdate);
    socket.on('call_ended', handleCallEnded);
    return () => {
      socket.off('call_update', handleCallUpdate);
      socket.off('call_ended', handleCallEnded);
    };
  }, [socket, currentCallSid]);

  // Listen for transcription updates via WebSocket
  useEffect(() => {
    if (!effectiveCallSid || !socket) return;

    console.log('📝 [ContactDetail] Subscribing to transcription for call:', effectiveCallSid);

    // Fetch existing transcription history for this call
    api.get(`/api/calls/${effectiveCallSid}`)
      .then(res => {
        const existing = res.data.transcriptions || [];
        if (existing.length > 0) {
          console.log(`📝 [ContactDetail] Loaded ${existing.length} existing transcriptions`);
          setTranscription(existing.map((t: any) => ({
            id: String(t.id || t.sequence_number),
            speaker: t.speaker || 'caller',
            text: t.transcript || t.text,
            timestamp: t.created_at || new Date().toISOString(),
          })));
        }
      })
      .catch(err => {
        console.debug('📝 [ContactDetail] Could not load transcription history:', err.message);
      });

    // Join the call room to receive events for this specific call
    const token = localStorage.getItem('access_token');
    if (token) {
      socket.emit('join_call', {
        call_sid: effectiveCallSid,
        token: token
      });
      console.log('📝 [ContactDetail] Joined call room:', effectiveCallSid);
    }

    const handleTranscription = (data: any) => {
      // Match by call_sid (could be SignalWire ID or our internal ID)
      if (data.call_sid === effectiveCallSid || data.call_id === effectiveCallSid) {
        console.log('📝 [ContactDetail] Received transcription:', data);
        // Map speaker from backend's 'speaker' field, or fallback to mapping 'role'
        // role: 'remote-caller' = caller, 'local-caller' = agent/AI
        let speaker = data.speaker;
        if (!speaker && data.role) {
          speaker = data.role === 'remote-caller' ? 'caller' : 'agent';
        }
        setTranscription(prev => {
          // Deduplicate: skip if last entry has same text and speaker
          const last = prev[prev.length - 1];
          if (last && last.text === data.text && last.speaker === (speaker || 'caller')) return prev;
          return [...prev, {
            id: `${Date.now()}`,
            speaker: speaker || 'caller',
            text: data.text,
            timestamp: new Date().toISOString(),
          }];
        });
      }
    };

    socket.on('transcription', handleTranscription);

    return () => {
      socket.off('transcription', handleTranscription);
      // Leave the call room when component unmounts or call changes
      socket.emit('leave_call', { call_sid: effectiveCallSid });
    };
  }, [effectiveCallSid, socket]);

  // Auto-switch to live tab when call starts (including inbound AI calls and outbound browser calls)
  useEffect(() => {
    if (hasAnyActiveCall) {
      console.log('📞 [ContactDetail] Active call detected, switching to live tab. callState:', callState);
      setActiveTab('live');
    }
  }, [hasAnyActiveCall, callState]);

  // Track previous call state to detect when call ends
  const prevActiveCallRef = useRef(activeCallForContact);
  useEffect(() => {
    const hadActiveCall = prevActiveCallRef.current;
    const hasActiveCall = activeCallForContact;

    // Call just ended - show the call detail view
    if (hadActiveCall && !hasActiveCall) {
      console.log('📞 [ContactDetail] Call ended, loading interactions and opening detail view');
      // Reload interactions to get the just-completed call
      const loadAndSelectCall = async () => {
        try {
          const response = await contactsApi.getInteractions(contact.id, 1, 20);
          const newInteractions = response.data.interactions;
          setInteractions(newInteractions);

          // Select the most recent call (should be the one that just ended)
          if (newInteractions.length > 0) {
            setSelectedHistoryCall(newInteractions[0]);
            setActiveTab('callDetail');
          } else {
            setActiveTab('history');
          }
        } catch (error) {
          console.error('Failed to load interactions after call ended:', error);
          setActiveTab('history');
        }
      };
      loadAndSelectCall();
    }

    prevActiveCallRef.current = activeCallForContact;
  }, [activeCallForContact, contact.id]);

  // Close menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (moreMenuRef.current && !moreMenuRef.current.contains(event.target as Node)) {
        setShowMoreMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Fetch available AI agents on mount
  useEffect(() => {
    api.get('/api/ai/agents')
      .then(res => {
        setAvailableAgents(res.data.agents);
        if (res.data.agents.length > 0) {
          setAiFormData(prev => ({ ...prev, agentType: res.data.agents[0].id }));
        }
      })
      .catch(err => console.error('Failed to fetch AI agents:', err));
  }, []);

  const handleDeleteContact = async () => {
    if (!confirm(`Delete ${contact.displayName}? This will also delete all call history for this contact.`)) {
      return;
    }

    setIsDeleting(true);
    try {
      await contactsApi.delete(contact.id);
      setShowMoreMenu(false);
      onContactDelete?.(contact.id);
    } catch (error) {
      console.error('Failed to delete contact:', error);
      alert('Failed to delete contact');
    } finally {
      setIsDeleting(false);
    }
  };

  const loadInteractions = async () => {
    setIsLoadingInteractions(true);
    try {
      const response = await contactsApi.getInteractions(contact.id, 1, 20);
      setInteractions(response.data.interactions);
    } catch (error) {
      console.error('Failed to load interactions:', error);
    } finally {
      setIsLoadingInteractions(false);
    }
  };

  const [dialError, setDialError] = useState<string | null>(null);

  const handleCall = async () => {
    setDialError(null);

    // If not online, need to go online first (for inbound handling)
    if (!isOnline) {
      console.log('📞 [ContactDetail] Going online first...');
      await goOnline();
    }

    try {
      // Pass contact context to the call
      const context = {
        contact_id: contact.id,
        contact_name: contact.displayName,
        account_tier: contact.accountTier,
        is_vip: contact.isVip,
        total_calls: contact.totalCalls,
        company: contact.company,
      };

      console.log('📞 [ContactDetail] Initiating call to:', contact.phone);
      setIsAICall(false);
      setTranscription([]);
      const result = await makeCall(contact.phone, context);
      console.log('📞 [ContactDetail] Call initiated:', result);
    } catch (error: any) {
      console.error('❌ [ContactDetail] Failed to initiate call:', error);
      setDialError(error?.message || 'Failed to initiate call');
    }
  };

  const handleSendAI = () => {
    // Pre-fill form fields from contact data
    setAiFormData(prev => ({
      ...prev,
      contactName: contact.displayName || '',
      company: contact.company || '',
      accountTier: contact.accountTier || 'free',
      isVip: contact.isVip || false,
      additionalContext: '',
    }));
    setShowAIForm(true);
    setAiFormError(null);
  };

  const handleSubmitAIForm = async () => {
    setIsSubmittingAI(true);
    setAiFormError(null);

    try {
      const response = await api.post('/api/ai/outbound-call', {
        phone: contact.phone,
        contact_id: contact.id,
        agent_type: aiFormData.agentType,
        context: {
          contact_name: aiFormData.contactName,
          account_tier: aiFormData.accountTier,
          is_vip: aiFormData.isVip,
          company: aiFormData.company || undefined,
          total_calls: contact.totalCalls,
          notes: contact.notes,
          additional_context: aiFormData.additionalContext || undefined,
        }
      });

      if (response.data.success) {
        setIsAICall(true);
        setCurrentCallSid(response.data.call_sid);
        setTranscription([]);
        setShowAIForm(false);
      }
    } catch (error: any) {
      console.error('Failed to send AI agent:', error);
      setAiFormError(error?.response?.data?.error || error?.message || 'Failed to initiate AI call');
    } finally {
      setIsSubmittingAI(false);
    }
  };

  const handleEndCall = async () => {
    try {
      // Disconnect our Call Fabric leg first if we're connected
      if (activeCall) {
        try {
          await hangup();
        } catch (err) {
          console.warn('📞 [ContactDetail] hangup() failed (may already be disconnected):', err);
        }
      }

      // Always end the call via backend API so the phone call is terminated
      // and the frontend gets proper socket events to clean up
      const callId = activeCallForContact?.id
        || activeCallForContact?.signalwire_call_sid
        || (activeCallForContact as any)?.signalwireCallSid;
      const callSid = effectiveCallSid || callId;

      if (callSid) {
        console.log('📞 [ContactDetail] Ending call via API:', callSid);
        await api.post(`/api/calls/${callSid}/end`);
      }
    } catch (error) {
      console.error('Failed to end call:', error);
    }
    setTranscription([]);
    setCurrentCallSid(null);
    setIsAICall(false);
    // Reload interactions to show the new call in history
    loadInteractions();
  };

  // Handle taking over an AI call
  const handleTakeOver = async () => {
    const callSid = effectiveCallSid;
    if (!callSid) {
      console.error('No call SID available for takeover');
      return;
    }

    // Go online if not already
    if (!isOnline) {
      await goOnline();
    }

    try {
      console.log('📞 [TakeOver] Initiating takeover for call:', callSid);

      // Call the takeover API to get the resource dial address (includes token)
      const response = await api.post(`/api/calls/${callSid}/takeover`);
      const { dial_address, leg_id } = response.data;

      console.log('📞 [TakeOver] Got dial address:', dial_address);

      // Dial the resource address — token is embedded in the URL
      // The SWML will use execute_rpc to end AI + connect to call:{sid}
      await makeCallToSwml(dial_address, {
        contact_id: contact.id,
        original_call_sid: callSid,
        leg_id: leg_id
      });

      // Update state - no longer an AI call once we've taken over
      setIsAICall(false);
      console.log('📞 [TakeOver] Successfully initiated takeover');

    } catch (error: any) {
      console.error('Failed to take over call:', error);
      const errorMessage = error.response?.data?.error || 'Failed to take over call';
      console.error('Error details:', errorMessage);
    }
  };

  // Handle selecting a call from history
  const handleSelectHistoryCall = (interaction: Interaction) => {
    setSelectedHistoryCall(interaction);
    setActiveTab('callDetail');
  };

  // Handle closing the call detail tab
  const handleCloseCallDetail = () => {
    setSelectedHistoryCall(null);
    setActiveTab('history');
  };

  const formatCallDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return 'Never';
    const date = new Date(dateString);
    if (isNaN(date.getTime())) return 'Invalid date';

    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));

    // Handle future dates or same day
    if (days < 0) {
      return 'Just now';  // Future date (likely timezone issue)
    } else if (days === 0) {
      return 'Today';
    } else if (days === 1) {
      return 'Yesterday';
    } else if (days < 7) {
      return `${days} days ago`;
    } else {
      return date.toLocaleDateString();
    }
  };

  const formatDuration = (seconds?: number) => {
    if (!seconds) return '--';
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className="h-full flex flex-col bg-canvas">
      {/* Header — subtle raised band, distinct from page */}
      <div className="bg-canvas-raised border-b border-rule p-5">
        <div className="flex items-start gap-4">
          {/* Avatar — flat, monogram */}
          <div className="w-14 h-14 rounded bg-canvas-raised border border-rule-strong flex items-center justify-center text-ink text-[22px] font-semibold tracking-tight">
            {contact.displayName.charAt(0).toUpperCase()}
          </div>

          {/* Basic Info */}
          <div className="flex-1 min-w-0">
            <div className="kicker mb-1">Contact</div>
            <div className="flex items-center gap-2">
              <h2 className="font-display text-[32px] leading-none text-ink tracking-tightest truncate">
                {contact.displayName}
              </h2>
              {contact.isVip && (
                <Star className="w-4 h-4 text-wait fill-wait flex-shrink-0" />
              )}
              {contact.isBlocked && (
                <span className="chip chip-urgent"><Ban className="w-2.5 h-2.5" />Blocked</span>
              )}
            </div>
            <div className="flex items-center gap-4 mt-2 text-[13px]">
              <span className="flex items-center gap-1.5 text-ink-muted">
                <Phone className="w-3.5 h-3.5 text-ink-dim" />
                <span className="mono">{contact.phone}</span>
              </span>
              {contact.email && (
                <span className="flex items-center gap-1.5 text-ink-muted">
                  <Mail className="w-3.5 h-3.5 text-ink-dim" />
                  {contact.email}
                </span>
              )}
              {contact.company && (
                <span className="flex items-center gap-1.5 text-ink-muted">
                  <Building2 className="w-3.5 h-3.5 text-ink-dim" />
                  {contact.company}{contact.jobTitle && <span className="text-ink-dim"> · {contact.jobTitle}</span>}
                </span>
              )}
            </div>
          </div>

          {/* Edit Button */}
          <button
            onClick={() => setIsEditing(true)}
            className="btn-ghost !p-2"
            title="Edit contact"
          >
            <Edit2 className="w-4 h-4" />
          </button>
        </div>

        {/* Queue Status Banner */}
        {isCallInQueue && (
          <div className={`mt-4 p-3 rounded border ${
            isUrgent
              ? 'bg-urgent/10 border-urgent/30'
              : queueStatus === 'assigned'
              ? 'bg-info/10 border-info/30'
              : 'bg-wait/10 border-wait/30'
          }`}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className={`w-9 h-9 rounded flex items-center justify-center ${
                  isUrgent ? 'bg-urgent/15 border border-urgent/30' : 'bg-wait/15 border border-wait/30'
                }`}>
                  <Users className={`w-4 h-4 ${isUrgent ? 'text-urgent-soft' : 'text-wait-soft'}`} />
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <span className={`font-medium text-[13px] ${isUrgent ? 'text-urgent-soft' : queueStatus === 'assigned' ? 'text-info-soft' : 'text-wait-soft'}`}>
                      {isUrgent ? 'Urgent · ' : ''}
                      {queueStatus === 'assigned' ? 'Assigned to you' : 'In queue'}
                    </span>
                    {activeCallForContact?.queue_id && (
                      <span className="chip chip-muted">{activeCallForContact.queue_id}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-[12px] text-ink-dim mt-0.5">
                    {waitTime !== undefined && waitTime > 0 && (
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        <span className="mono">Waiting {formatWaitTime(waitTime)}</span>
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <button
                onClick={handleTakeQueuedCall}
                disabled={isTakingCall || !isClientReady}
                className="btn-primary"
              >
                <PhoneCall className="w-3.5 h-3.5" />
                {isTakingCall ? 'Connecting…' : 'Take call'}
              </button>
            </div>
            {takeCallError && (
              <div className="mt-2 p-2 bg-urgent/15 border border-urgent/30 rounded text-urgent-soft text-[12px] flex items-center gap-2">
                <AlertCircle className="w-3.5 h-3.5" />
                {takeCallError}
              </div>
            )}
          </div>
        )}

        {/* Action Buttons / Call Controls */}
        <div className="flex items-center gap-2 mt-4">
          {hasAnyActiveCall ? (
            <>
              {/* Live duration pill */}
              <div className={`flex items-center gap-2.5 px-3 py-1.5 rounded border ${
                isAICall || isInboundAICall
                  ? 'bg-ai/10 border-ai/30'
                  : callState === 'ringing'
                  ? 'bg-wait/10 border-wait/30'
                  : 'bg-live/10 border-live/30'
              }`}>
                <span className={`dot ${
                  isAICall || isInboundAICall ? 'dot-ai' : callState === 'ringing' ? 'dot-wait' : 'dot-live'
                }`} />
                <span className={`mono text-[13px] font-medium ${
                  isAICall || isInboundAICall ? 'text-ai-soft' : callState === 'ringing' ? 'text-wait-soft' : 'text-live-soft'
                }`}>
                  {isOutboundCallInProgress && callState !== 'active'
                    ? getOutboundCallStatus()
                    : formatCallDuration(activeCallForContact?.duration || callDuration)
                  }
                </span>
                {(isAICall || isInboundAICall) && (
                  <span className="chip chip-ai"><Bot className="w-2.5 h-2.5" />AI Agent</span>
                )}
              </div>

              {!isAICall && !isInboundAICall && activeCall && (
                <button
                  onClick={() => isMuted ? unmute() : mute()}
                  className={`btn-secondary ${isMuted ? '!bg-wait/15 !border-wait/30 !text-wait-soft' : ''}`}
                >
                  {isMuted ? <MicOff className="w-3.5 h-3.5" /> : <Mic className="w-3.5 h-3.5" />}
                  {isMuted ? 'Unmute' : 'Mute'}
                </button>
              )}

              {(isAICall || isInboundAICall) && (
                <button
                  onClick={handleTakeOver}
                  disabled={isInitializing}
                  className="btn-secondary !border-info/30 !text-info-soft hover:!bg-info/10"
                >
                  <Phone className="w-3.5 h-3.5" />
                  Take over
                </button>
              )}

              <button onClick={handleEndCall} className="btn-danger">
                <PhoneOff className="w-3.5 h-3.5" />
                End call
              </button>
              <CallControlPanel
                callId={activeCallForContact?.id || currentCallSid || ''}
                callSid={effectiveCallSid || ''}
                isAICall={isAICall || isInboundAICall}
                isHumanCall={!isAICall && !isInboundAICall}
                isInConference={isInConference}
                isOnHold={isOnHold}
                isRecording={isRecording}
                userRole={currentUser?.role}
                onHoldChange={setIsOnHold}
                onRecordingChange={setIsRecording}
              />
              {isInConference && conferenceParticipants.length > 0 && (
                <ConferenceParticipants
                  participants={conferenceParticipants}
                  className="mt-3"
                />
              )}
            </>
          ) : (
            <>
              <button onClick={handleCall} disabled={isInitializing} className="btn-primary">
                <Phone className="w-3.5 h-3.5" />
                {isInitializing ? 'Connecting…' : 'Call'}
              </button>
              <button onClick={handleSendAI} className="btn-secondary !border-ai/30 !text-ai-soft hover:!bg-ai/10">
                <Bot className="w-3.5 h-3.5" />
                Send AI agent
              </button>
              {contact.email && (
                <button
                  onClick={() => window.open(`mailto:${contact.email}`)}
                  className="btn-ghost"
                >
                  <Mail className="w-3.5 h-3.5" />
                  Email
                </button>
              )}
            </>
          )}

          {dialError && (
            <div className="flex items-center gap-2 px-3 py-1 bg-urgent/10 border border-urgent/30 rounded text-urgent-soft text-[12px]">
              <span>{dialError}</span>
              <button onClick={() => setDialError(null)} className="ml-1 hover:text-ink">✕</button>
            </div>
          )}

          <div className="relative ml-auto" ref={moreMenuRef}>
            <button
              onClick={() => setShowMoreMenu(!showMoreMenu)}
              className="btn-ghost !p-2"
            >
              <MoreHorizontal className="w-4 h-4" />
            </button>
            {showMoreMenu && (
              <div className="absolute right-0 mt-1 w-48 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
                <button
                  onClick={handleDeleteContact}
                  disabled={isDeleting}
                  className="w-full flex items-center gap-2 px-3 py-2 text-urgent-soft hover:bg-canvas-hover transition-colors disabled:opacity-50 text-[13px]"
                >
                  <Trash2 className="w-4 h-4" />
                  {isDeleting ? 'Deleting…' : 'Delete contact'}
                </button>
              </div>
            )}
          </div>
        </div>

        {callError && (
          <div className="mt-2 p-2 bg-urgent/10 border border-urgent/30 rounded text-urgent-soft text-[12px] flex items-center gap-2">
            <AlertCircle className="w-3.5 h-3.5" />
            {callError}
          </div>
        )}

        {/* AI Agent Configuration Form */}
        {showAIForm && !hasAnyActiveCall && (
          <div className="mt-4 p-4 bg-ai/5 border border-ai/25 rounded">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <Bot className="w-3.5 h-3.5 text-ai-soft" />
                <span className="kicker" style={{ color: '#B0A4FF' }}>Dispatch AI agent</span>
              </div>
              <button
                onClick={() => setShowAIForm(false)}
                className="btn-ghost !p-1"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="mb-3">
              <label className="block kicker mb-1">Agent</label>
              <select
                value={aiFormData.agentType}
                onChange={(e) => setAiFormData({ ...aiFormData, agentType: e.target.value })}
                className="input"
              >
                {availableAgents.map(agent => (
                  <option key={agent.id} value={agent.id}>{agent.name}</option>
                ))}
              </select>
              {availableAgents.find(a => a.id === aiFormData.agentType)?.description && (
                <p className="text-[11.5px] text-ink-dim mt-1">
                  {availableAgents.find(a => a.id === aiFormData.agentType)?.description}
                </p>
              )}
            </div>

            <div className="mb-3">
              <label className="block kicker mb-2">Context sent to AI</label>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="block text-[10.5px] text-ink-dim mb-1 uppercase tracking-wider">Name</label>
                  <input type="text" className="input !py-1.5"
                    value={aiFormData.contactName}
                    onChange={(e) => setAiFormData({ ...aiFormData, contactName: e.target.value })} />
                </div>
                <div>
                  <label className="block text-[10.5px] text-ink-dim mb-1 uppercase tracking-wider">Company</label>
                  <input type="text" className="input !py-1.5"
                    value={aiFormData.company}
                    onChange={(e) => setAiFormData({ ...aiFormData, company: e.target.value })} />
                </div>
                <div>
                  <label className="block text-[10.5px] text-ink-dim mb-1 uppercase tracking-wider">Tier</label>
                  <select
                    value={aiFormData.accountTier}
                    onChange={(e) => setAiFormData({ ...aiFormData, accountTier: e.target.value })}
                    className="input !py-1.5"
                  >
                    <option value="prospect">Prospect</option>
                    <option value="free">Free</option>
                    <option value="pro">Pro</option>
                    <option value="enterprise">Enterprise</option>
                  </select>
                </div>
                <div className="flex items-end pb-1">
                  <label className="flex items-center gap-2 cursor-pointer text-[13px]">
                    <input type="checkbox" className="w-3.5 h-3.5 rounded-sm accent-sw-blue"
                      checked={aiFormData.isVip}
                      onChange={(e) => setAiFormData({ ...aiFormData, isVip: e.target.checked })} />
                    <span className={aiFormData.isVip ? 'text-wait-soft' : 'text-ink-muted'}>
                      <Star className="w-3 h-3 inline mr-1" />VIP
                    </span>
                  </label>
                </div>
              </div>
            </div>

            <div className="mb-3">
              <label className="block kicker mb-1">Additional context (optional)</label>
              <textarea
                value={aiFormData.additionalContext}
                onChange={(e) => setAiFormData({ ...aiFormData, additionalContext: e.target.value })}
                placeholder="e.g., Follow up on previous quote, ask about renewal..."
                className="input resize-none"
                rows={2}
              />
            </div>

            {aiFormError && (
              <div className="mb-3 p-2 bg-urgent/10 border border-urgent/30 rounded text-urgent-soft text-[11.5px] flex items-center gap-2">
                <AlertCircle className="w-3 h-3" />
                {aiFormError}
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={handleSubmitAIForm}
                disabled={isSubmittingAI}
                className="btn-secondary !border-ai/30 !text-ai-soft hover:!bg-ai/10"
              >
                {isSubmittingAI ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Bot className="w-3.5 h-3.5" />}
                {isSubmittingAI ? 'Sending…' : 'Send agent'}
              </button>
              <button onClick={() => setShowAIForm(false)} className="btn-ghost">Cancel</button>
            </div>
          </div>
        )}

        {/* Tags */}
        {contact.tags && contact.tags.length > 0 && (
          <div className="flex items-center gap-2 mt-4">
            <Tag className="w-3.5 h-3.5 text-ink-dim" />
            <div className="flex flex-wrap gap-1">
              {contact.tags.map((tag, index) => (
                <span key={index} className="chip chip-muted">{tag}</span>
              ))}
            </div>
          </div>
        )}

        {/* Quick Stats — inline, no grid cells, no dividers. Pure typography rhythm. */}
        <div className="mt-6 flex items-baseline gap-10 flex-wrap">
          <HeroStat kicker="Total calls" value={String(contact.totalCalls)} />
          <HeroStat
            kicker="Avg sentiment"
            value={contact.averageSentiment != null ? (contact.averageSentiment > 0 ? '+' : '') + contact.averageSentiment.toFixed(1) : '—'}
            tone={
              contact.averageSentiment != null
                ? contact.averageSentiment > 0.3 ? 'live'
                  : contact.averageSentiment < -0.3 ? 'urgent'
                  : 'default'
                : 'default'
            }
          />
          <HeroStat kicker="Tier" value={contact.accountTier} isTier />
          <HeroStat kicker="Last contact" value={formatDate(contact.lastInteractionAt)} isSmall />
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center px-5 h-11 gap-1">
        {hasAnyActiveCall && (
          <DetailTab
            active={activeTab === 'live'}
            onClick={() => setActiveTab('live' as any)}
            tone={isInboundAICall || isAICall ? 'ai' : callState === 'ringing' ? 'wait' : 'live'}
            label={
              isInboundAICall || isAICall
                ? 'AI Call'
                : isOutboundCallInProgress && callState !== 'active'
                  ? getOutboundCallStatus()
                  : 'Live Call'
            }
            icon={
              isInboundAICall || isAICall
                ? <Bot className="w-3.5 h-3.5" />
                : isOutboundCallInProgress && callState !== 'active'
                  ? <PhoneOutgoing className="w-3.5 h-3.5" />
                  : <span className={`dot ${callState === 'ringing' ? 'dot-wait' : 'dot-live'}`} />
            }
          />
        )}
        <DetailTab
          active={activeTab === 'history'}
          onClick={() => setActiveTab('history')}
          label="Call History"
          icon={<Clock className="w-3.5 h-3.5" />}
        />
        {selectedHistoryCall && (
          <DetailTab
            active={activeTab === 'callDetail'}
            onClick={() => setActiveTab('callDetail')}
            tone="signal"
            label="Call Detail"
            icon={<FileText className="w-3.5 h-3.5" />}
            onClose={handleCloseCallDetail}
          />
        )}
        <DetailTab
          active={activeTab === 'notes'}
          onClick={() => setActiveTab('notes')}
          label="Notes"
          icon={<MessageSquare className="w-3.5 h-3.5" />}
        />
        <DetailTab
          active={activeTab === 'details'}
          onClick={() => setActiveTab('details')}
          label="Details"
          icon={<User className="w-3.5 h-3.5" />}
        />
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-y-auto">
        {activeTab === 'live' && hasAnyActiveCall && (
          <LiveCallTab
            transcription={transcription}
            isAICall={isAICall || isInboundAICall}
            callSid={effectiveCallSid || activeCall?.id}
            callDuration={activeCallForContact?.duration || callDuration}
            callState={callState}
            isOutboundCallInProgress={isOutboundCallInProgress}
            aiContext={connectedCustomer?.aiContext || (activeCallForContact as any)?.aiContext}
            sentiment={liveSentiment}
          />
        )}
        {activeTab === 'history' && (
          <InteractionHistory
            interactions={interactions}
            isLoading={isLoadingInteractions}
            formatDate={formatDate}
            formatDuration={formatDuration}
            onSelectCall={handleSelectHistoryCall}
          />
        )}
        {activeTab === 'callDetail' && selectedHistoryCall && (
          <CallDetailTab
            interaction={selectedHistoryCall}
            formatDate={formatDate}
            formatDuration={formatDuration}
          />
        )}
        {activeTab === 'notes' && (
          <NotesTab contact={contact} onUpdate={onContactUpdate} />
        )}
        {activeTab === 'details' && (
          <DetailsTab contact={contact} />
        )}
      </div>

      {/* Edit Modal */}
      {isEditing && (
        <EditContactModal
          contact={contact}
          onClose={() => setIsEditing(false)}
          onSave={onContactUpdate}
        />
      )}
    </div>
  );
}

function HeroStat({
  kicker,
  value,
  tone = 'default',
  isTier,
  isSmall,
}: {
  kicker: string;
  value: string;
  tone?: 'default' | 'live' | 'urgent';
  isTier?: boolean;
  isSmall?: boolean;
}) {
  const color = tone === 'live' ? 'text-live-soft' : tone === 'urgent' ? 'text-urgent-soft' : 'text-ink';
  return (
    <div className="flex flex-col gap-1">
      <span className="kicker">{kicker}</span>
      {isTier ? (
        <span className={`font-heading font-semibold text-[20px] capitalize leading-none ${color}`}>{value}</span>
      ) : isSmall ? (
        <span className={`font-heading font-semibold text-[17px] leading-none mono ${color}`}>{value}</span>
      ) : (
        <span className={`font-heading font-semibold text-[26px] leading-none tabular-nums ${color}`}>{value}</span>
      )}
    </div>
  );
}

function DetailTab({
  active,
  onClick,
  label,
  icon,
  tone = 'default',
  onClose,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  icon?: React.ReactNode;
  tone?: 'default' | 'live' | 'ai' | 'wait' | 'signal';
  onClose?: () => void;
}) {
  const activeColor =
    tone === 'live'   ? 'text-live-soft'   :
    tone === 'ai'     ? 'text-ai-soft'     :
    tone === 'wait'   ? 'text-wait-soft'   :
    tone === 'signal' ? 'text-sw-turquoise' :
    'text-ink';
  const underline =
    tone === 'live'   ? 'bg-live'   :
    tone === 'ai'     ? 'bg-ai'     :
    tone === 'wait'   ? 'bg-wait'   :
    'bg-sw-turquoise';
  return (
    <button
      onClick={onClick}
      className={`relative h-10 px-3 flex items-center gap-1.5 text-[12.5px] font-medium transition-colors ${
        active ? activeColor : 'text-ink-dim hover:text-ink-muted'
      }`}
    >
      {icon}
      <span>{label}</span>
      {onClose && (
        <span
          onClick={(e) => { e.stopPropagation(); onClose(); }}
          className="ml-1 p-0.5 hover:bg-canvas-hover rounded-sm cursor-pointer"
          title="Close"
        >
          <X className="w-2.5 h-2.5" />
        </span>
      )}
      {active && (
        <span className={`absolute -bottom-[1px] left-2 right-2 h-[2px] ${underline} rounded-sm`} />
      )}
    </button>
  );
}

function InteractionHistory({
  interactions,
  isLoading,
  formatDate,
  formatDuration,
  onSelectCall,
}: {
  interactions: Interaction[];
  isLoading: boolean;
  formatDate: (date?: string) => string;
  formatDuration: (seconds?: number) => string;
  onSelectCall: (interaction: Interaction) => void;
}) {
  if (isLoading) {
    return (
      <div className="p-8 text-center text-ink-dim">
        <Loader2 className="w-5 h-5 mx-auto mb-2 animate-spin" />
        <span className="text-[12px]">Loading history…</span>
      </div>
    );
  }

  if (interactions.length === 0) {
    return (
      <div className="p-10 text-center">
        <Clock className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
        <p className="font-display text-[20px] text-ink-muted mb-1">No history yet</p>
        <p className="text-[12px] text-ink-dim">Calls with this contact will show up here.</p>
      </div>
    );
  }

  const renderHandlerChain = (interaction: Interaction) => {
    const legs = interaction.legs;
    if (!legs || legs.length === 0) {
      if (interaction.handlerType === 'ai') {
        return (
          <span className="inline-flex items-center gap-1 text-[11px]">
            <Bot className="w-3 h-3 text-ai-soft" />
            <span className="text-ink-muted">{interaction.aiAgentName || 'AI'}</span>
          </span>
        );
      }
      return null;
    }
    if (legs.length === 1) {
      const leg = legs[0];
      const isAI = leg.legType === 'ai_agent';
      return (
        <span className="inline-flex items-center gap-1 text-[11px]">
          {isAI ? <Bot className="w-3 h-3 text-ai-soft" /> : <User className="w-3 h-3 text-live-soft" />}
          <span className="text-ink-muted">{isAI ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}</span>
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 text-[11px]">
        {legs.map((leg, idx) => {
          const isAI = leg.legType === 'ai_agent';
          return (
            <span key={leg.id} className="inline-flex items-center gap-1">
              {isAI ? <Bot className="w-3 h-3 text-ai-soft" /> : <User className="w-3 h-3 text-live-soft" />}
              <span className="text-ink-muted">{isAI ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}</span>
              {idx < legs.length - 1 && <span className="text-ink-faint mx-1">→</span>}
            </span>
          );
        })}
      </span>
    );
  };

  return (
    <div>
      {interactions.map((interaction, idx) => {
        const sentimentTone =
          interaction.sentimentScore == null ? null :
          interaction.sentimentScore > 0.3 ? 'text-live-soft' :
          interaction.sentimentScore < -0.3 ? 'text-urgent-soft' :
          'text-ink-muted';
        return (
          <div key={interaction.id}>
            <div
              className="px-5 py-5 hover:bg-canvas-raised/40 transition-colors cursor-pointer"
              onClick={() => onSelectCall(interaction)}
            >
              <div className="flex items-start gap-3">
                {/* Direction glyph — minimal, muted */}
                <div className="mt-0.5 text-ink-dim flex-shrink-0">
                  {interaction.direction === 'inbound' ? (
                    <PhoneIncoming className="w-4 h-4" />
                  ) : (
                    <PhoneOutgoing className="w-4 h-4" />
                  )}
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium text-ink text-[13.5px]">
                      {interaction.direction === 'inbound' ? 'Inbound' : 'Outbound'}
                    </span>
                    <span className="text-ink-dim text-[12px]">·</span>
                    <span className="mono text-[11.5px] text-ink-muted">{formatDate(interaction.createdAt)}</span>
                    {interaction.duration && (
                      <>
                        <span className="text-ink-dim text-[12px]">·</span>
                        <span className="mono text-[11.5px] text-ink-muted">{formatDuration(interaction.duration)}</span>
                      </>
                    )}
                    {sentimentTone && (
                      <>
                        <span className="text-ink-dim text-[12px]">·</span>
                        <span className={`mono text-[11.5px] font-medium tabular-nums ${sentimentTone}`}>
                          {interaction.sentimentScore! > 0 ? '+' : ''}{interaction.sentimentScore!.toFixed(1)}
                        </span>
                      </>
                    )}
                    {interaction.status && interaction.status !== 'completed' && (
                      <>
                        <span className="text-ink-dim text-[12px]">·</span>
                        <span className="text-[11.5px] text-ink-muted capitalize">{interaction.status.replace('_', ' ')}</span>
                      </>
                    )}
                  </div>

                  {/* Handler chain — only if multi-leg, thin line */}
                  {interaction.legs && interaction.legs.length > 1 && (
                    <div className="mt-1.5 flex items-center gap-1 text-[11px] text-ink-muted">
                      {renderHandlerChain(interaction)}
                    </div>
                  )}

                  {/* Description — flows as paragraph, no box */}
                  {interaction.summary && (
                    <div className="mt-2 text-[12.5px] text-ink-muted leading-relaxed pr-8">
                      <AISummaryDisplay summary={interaction.summary} />
                    </div>
                  )}
                </div>
              </div>
            </div>
            {/* Short divider between entries — fixed left offset, consistent width */}
            {idx < interactions.length - 1 && (
              <div className="ml-[272px] h-px w-32 rule-fade" aria-hidden />
            )}
          </div>
        );
      })}
    </div>
  );
}

function NotesTab({
  contact,
  onUpdate,
}: {
  contact: Contact;
  onUpdate: (contact: Contact) => void;
}) {
  const [notes, setNotes] = useState(contact.notes || '');
  const [isSaving, setIsSaving] = useState(false);

  const handleSave = async () => {
    setIsSaving(true);
    try {
      const response = await contactsApi.update(contact.id, { notes });
      onUpdate(response.data);
    } catch (error) {
      console.error('Failed to save notes:', error);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="p-5">
      <div className="kicker mb-2">Notes</div>
      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="What should the next person who talks to this contact know?"
        className="input h-56 resize-none leading-relaxed"
      />
      <div className="flex justify-end mt-3">
        <button
          onClick={handleSave}
          disabled={isSaving || notes === (contact.notes || '')}
          className="btn-primary"
        >
          {isSaving ? 'Saving…' : 'Save notes'}
        </button>
      </div>
    </div>
  );
}

function DetailsTab({ contact }: { contact: Contact }) {
  return (
    <div className="p-5">
      <div className="kicker mb-3">Details</div>
      <div className="panel rounded-md">
        <DetailRow label="First name"    value={contact.firstName} />
        <DetailRow label="Last name"     value={contact.lastName} />
        <DetailRow label="Display name"  value={contact.displayName} />
        <DetailRow label="Phone"         value={contact.phone} mono />
        <DetailRow label="Email"         value={contact.email} />
        <DetailRow label="Company"       value={contact.company} />
        <DetailRow label="Job title"     value={contact.jobTitle} />
        <DetailRow label="Account tier"  value={contact.accountTier} />
        <DetailRow label="Status"        value={contact.accountStatus} />
        <DetailRow label="External ID"   value={contact.externalId} mono />
        <DetailRow label="VIP"           value={contact.isVip ? 'Yes' : 'No'} />
        <DetailRow label="Blocked"       value={contact.isBlocked ? 'Yes' : 'No'} />
        <DetailRow label="Created"       value={new Date(contact.createdAt).toLocaleString()} mono />
        <DetailRow label="Updated"       value={new Date(contact.updatedAt).toLocaleString()} mono />
      </div>

      {contact.customFields && Object.keys(contact.customFields).length > 0 && (
        <div className="mt-6">
          <div className="kicker mb-3">Custom fields</div>
          <div className="panel rounded-md">
            {Object.entries(contact.customFields).map(([key, value]) => (
              <DetailRow key={key} label={key} value={String(value)} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value, mono }: { label: string; value?: string; mono?: boolean }) {
  return (
    <div className="flex items-center px-3 py-2.5 border-b border-rule last:border-b-0 text-[13px]">
      <span className="w-36 text-ink-dim text-[11.5px] uppercase tracking-wider">{label}</span>
      <span className={`${mono ? 'mono' : ''} ${value ? 'text-ink' : 'text-ink-faint'}`}>
        {value || '—'}
      </span>
    </div>
  );
}

// LiveCallTab imported from ./LiveCallTab
// CallDetailTab imported from ./CallDetailTab

function EditContactModal({
  contact,
  onClose,
  onSave,
}: {
  contact: Contact;
  onClose: () => void;
  onSave: (contact: Contact) => void;
}) {
  const [formData, setFormData] = useState({
    firstName: contact.firstName || '',
    lastName: contact.lastName || '',
    displayName: contact.displayName,
    phone: contact.phone,
    email: contact.email || '',
    company: contact.company || '',
    jobTitle: contact.jobTitle || '',
    accountTier: contact.accountTier,
    isVip: contact.isVip,
    isBlocked: contact.isBlocked,
  });
  const [isSaving, setIsSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    try {
      const response = await contactsApi.update(contact.id, formData);
      onSave(response.data);
      onClose();
    } catch (error) {
      console.error('Failed to update contact:', error);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50 animate-fade-in">
      <div className="panel-raised rounded-md shadow-panel p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <div className="mb-5">
          <div className="kicker mb-1">Edit</div>
          <h2 className="font-display text-[26px] text-ink leading-none">Contact</h2>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5">
          <div className="grid grid-cols-2 gap-3">
            <ModalField label="First name">
              <input type="text" className="input"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })} />
            </ModalField>
            <ModalField label="Last name">
              <input type="text" className="input"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })} />
            </ModalField>
          </div>

          <ModalField label="Display name" required>
            <input type="text" className="input" required
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })} />
          </ModalField>

          <ModalField label="Phone" required>
            <input type="tel" className="input mono" required
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })} />
          </ModalField>

          <ModalField label="Email">
            <input type="email" className="input"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })} />
          </ModalField>

          <div className="grid grid-cols-2 gap-3">
            <ModalField label="Company">
              <input type="text" className="input"
                value={formData.company}
                onChange={(e) => setFormData({ ...formData, company: e.target.value })} />
            </ModalField>
            <ModalField label="Job title">
              <input type="text" className="input"
                value={formData.jobTitle}
                onChange={(e) => setFormData({ ...formData, jobTitle: e.target.value })} />
            </ModalField>
          </div>

          <ModalField label="Account tier">
            <select className="input"
              value={formData.accountTier}
              onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}>
              <option value="prospect">Prospect</option>
              <option value="free">Free</option>
              <option value="pro">Pro</option>
              <option value="enterprise">Enterprise</option>
            </select>
          </ModalField>

          <div className="flex items-center gap-6 pt-1">
            <label className="flex items-center gap-2 cursor-pointer text-[13px]">
              <input type="checkbox" className="w-3.5 h-3.5 rounded-sm accent-sw-blue"
                checked={formData.isVip}
                onChange={(e) => setFormData({ ...formData, isVip: e.target.checked })} />
              <span className="text-ink">VIP customer</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-[13px]">
              <input type="checkbox" className="w-3.5 h-3.5 rounded-sm accent-urgent"
                checked={formData.isBlocked}
                onChange={(e) => setFormData({ ...formData, isBlocked: e.target.checked })} />
              <span className="text-ink">Blocked</span>
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-4 border-t border-rule">
            <button type="button" onClick={onClose} className="btn-ghost">Cancel</button>
            <button type="submit" disabled={isSaving} className="btn-primary">
              {isSaving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ModalField({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block kicker mb-1">
        {label}{required && <span className="text-signal-soft ml-0.5">*</span>}
      </span>
      {children}
    </label>
  );
}

export default ContactDetailView;
