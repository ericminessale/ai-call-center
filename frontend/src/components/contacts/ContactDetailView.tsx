import { useState, useEffect, useRef } from 'react';
import {
  Phone,
  PhoneOff,
  Bot,
  Mail,
  Edit2,
  Star,
  Clock,
  ChevronDown,
  ChevronUp,
  Tag,
  Ban,
  MoreHorizontal,
  Trash2,
  Mic,
  MicOff,
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
import { PendingCallbackBanner } from './PendingCallbackBanner';
import { ConferenceParticipants } from './ConferenceParticipants';
import { ObserverControls } from '../shared/ObserverControls';
import { useAuthStore } from '../../stores/authStore';
import { useCallCapabilities } from '../../hooks/useCallCapabilities';
import { useContactPanelMode } from '../../hooks/useContactPanelMode';
import { logger } from '../../lib/logger';
import { Tabs, Chip, Button, PillBadge, CallHistoryRow, StatusDot, AI_GLYPH, type TabItem, type RestraintStatus } from '../restraint';

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

  // Published list rates for the live cost ticker (IMP-01) — one fetch per
  // mount; failure just hides the ticker.
  const [costRates, setCostRates] = useState<Record<string, number> | null>(null);
  useEffect(() => {
    callsApi.costRates().then((r) => setCostRates(r.data.rates)).catch(() => {});
  }, []);
  const [isAICall, setIsAICall] = useState(false);
  const [currentCallSid, setCurrentCallSid] = useState<string | null>(null);

  const inboundCallSid = activeCallForContact?.signalwire_call_sid || (activeCallForContact as any)?.call_sid;
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

  // Per-call capability set, used to gate transport-specific UI like
  // ObserverControls (Listen) and ConferenceParticipants. Bridge-mode calls
  // lack the multi-party capabilities so those surfaces auto-hide. See
  // CALL_TRANSPORT.md.
  const callCaps = useCallCapabilities(activeCallForContact);

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
      logger.debug('📋 [ContactDetail] Passing AI context to call:', aiContext);

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

      logger.debug('✅ [ContactDetail] Successfully took queued call');
    } catch (error: any) {
      logger.error('❌ [ContactDetail] Failed to take call:', error);
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

  // Panel mode is derived from the agent's SDK session, not from the inbound
  // Call row's mutable status field. See useContactPanelMode.ts for the full
  // precedence rules. The short version: if callState === 'active' or there's
  // an `activeCall`, panel goes human-active regardless of what the customer-
  // side Call row says — this is what stops the "AI controls stuck after
  // handoff" bug class.
  const panel = useContactPanelMode(activeCallForContact);

  // Outbound AI dispatched from this very page: while the backend's
  // call_update hasn't propagated yet, `isAICall` (local state, set in
  // handleSubmitAIForm) acts as a UI bridge. Once the row arrives, the
  // hook's Rule 4 (handler_type === 'ai') takes over and this term is
  // redundant. The `!showHumanControls` guard ensures that if the agent
  // somehow lands on a live human leg, we never show AI controls — the
  // SDK is the authoritative tie-breaker.
  const showAIControls = panel.showAIMonitor || (isAICall && !panel.showHumanControls);

  const isAgentConnected = isInConference && (callState === 'active' || callState === 'ringing');
  const isOutboundCallInProgress = panel.showRinging;
  const isAgentOnCall = panel.isAgentOnCall || (isAICall && !!currentCallSid);

  // Broader "is there any call activity tied to this contact" — kept for the
  // existing places that want to hide the AI-form / call-history selector
  // when a queued call exists for the contact.
  const hasAnyActiveCall = isAgentOnCall || !!activeCallForContact;

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

  // Load interactions when contact changes. Also reset any per-contact
  // drill-down state — without this, switching to a new contact while the
  // callDetail tab is active leaves the previous contact's call detail
  // rendered against the new contact's header (state bleed bug).
  useEffect(() => {
    loadInteractions();
    setSelectedHistoryCall(null);
    if (activeTab === 'callDetail') {
      setActiveTab('history');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- activeTab read for
    // conditional reset only; we don't want to re-fire on tab changes.
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
        logger.debug('📞 [ContactDetail] Call ended via socket event, clearing local state');
        setCurrentCallSid(null);
        setIsAICall(false);
        loadInteractions();
      }
    };

    const handleCallEnded = (data: { call_sid?: string; callId?: number }) => {
      if (data.call_sid === currentCallSid) {
        logger.debug('📞 [ContactDetail] call_ended event received, clearing local state');
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

    logger.debug('📝 [ContactDetail] Subscribing to transcription for call:', effectiveCallSid);

    // Fetch existing transcription history for this call
    api.get(`/api/calls/${effectiveCallSid}`)
      .then(res => {
        const existing = res.data.transcriptions || [];
        if (existing.length > 0) {
          logger.debug(`📝 [ContactDetail] Loaded ${existing.length} existing transcriptions`);
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
      logger.debug('📝 [ContactDetail] Joined call room:', effectiveCallSid);
    }

    const handleTranscription = (data: any) => {
      // Match by call_sid (could be SignalWire ID or our internal ID)
      if (data.call_sid === effectiveCallSid || data.call_id === effectiveCallSid) {
        logger.debug('📝 [ContactDetail] Received transcription:', data);
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

    // Recording state — backend emits `recording_status` from the SignalWire
    // recording webhook (in-progress / completed / failed) and `recording` when
    // a completed recording_url is available. Sync local state to whichever
    // arrives, so the button reflects reality even when the state change wasn't
    // initiated by this client.
    const handleRecordingStatus = (data: { call_sid?: string; status?: string }) => {
      if (data.call_sid !== effectiveCallSid) return;
      const s = (data.status || '').toLowerCase();
      if (s === 'recording' || s === 'in-progress') setIsRecording(true);
      else if (s === 'completed' || s === 'stopped' || s === 'failed' || s === 'no-input') {
        setIsRecording(false);
      }
    };
    const handleRecordingFinished = (data: { call_sid?: string }) => {
      if (data.call_sid !== effectiveCallSid) return;
      setIsRecording(false);
    };
    socket.on('recording_status', handleRecordingStatus);
    socket.on('recording', handleRecordingFinished);

    // RT-02 (2026-06-02 audit followup): Socket.IO room membership is
    // server-side state. When the socket disconnects (network blip, tab
    // sleep, server restart) and reconnects, the server's session for
    // this socket is brand new — the old room subscriptions are gone.
    // The Socket.IO client object itself is reused on reconnect, so the
    // outer effect doesn't re-fire and we never re-emit join_call.
    // Result: transcription panel goes dead, recording status button
    // gets stuck, agent doesn't know why until they refresh.
    //
    // Fix: subscribe to the socket's own `connect` event (fires on every
    // reconnect, NOT just the initial connect). Re-emit join_call so the
    // server re-adds us to the call-id room. Idempotent on the server
    // side — Flask-SocketIO join_room is a no-op when already a member.
    const handleSocketReconnect = () => {
      const token = localStorage.getItem('access_token');
      if (token) {
        socket.emit('join_call', { call_sid: effectiveCallSid, token });
        logger.debug('🔄 [ContactDetail] Socket reconnected → re-joined call room:', effectiveCallSid);
      }
    };
    socket.on('connect', handleSocketReconnect);

    return () => {
      socket.off('transcription', handleTranscription);
      socket.off('recording_status', handleRecordingStatus);
      socket.off('recording', handleRecordingFinished);
      socket.off('connect', handleSocketReconnect);
      // Leave the call room when component unmounts or call changes
      socket.emit('leave_call', { call_sid: effectiveCallSid });
    };
  }, [effectiveCallSid, socket]);

  // Auto-switch to live tab when call starts (including inbound AI calls and outbound browser calls)
  useEffect(() => {
    if (hasAnyActiveCall) {
      logger.debug('📞 [ContactDetail] Active call detected, switching to live tab. callState:', callState);
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
      logger.debug('📞 [ContactDetail] Call ended, loading interactions and opening detail view');
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
          logger.error('Failed to load interactions after call ended:', error);
          setActiveTab('history');
        }
      };
      loadAndSelectCall();
    }

    prevActiveCallRef.current = activeCallForContact;
  }, [activeCallForContact, contact.id]);

  // When a recording finishes AFTER the call has ended, SignalWire delivers
  // the URL via the /api/webhooks/recording webhook. The earlier socket
  // listener up at the transcription effect only fires while the call room
  // is still joined (effectiveCallSid populated). Once the call ends, that
  // listener tears down — but the recording URL often arrives a few seconds
  // later, which is what caused the "playback empty until refresh" bug.
  //
  // Listen for `recording` and `call_update` events at this higher scope
  // (lifetime = this ContactDetailView mount, not the call room). When
  // either delivers a recording URL for an interaction we currently have
  // in state — either selectedHistoryCall on the call-detail tab, or one
  // of the rows in the history list — patch it in place. No full refetch,
  // no flicker.
  useEffect(() => {
    if (!socket) return;

    const patchInteraction = (sid: string | undefined, recordingUrl: string) => {
      if (!sid || !recordingUrl) return;

      setInteractions((prev) =>
        prev.map((it) => {
          const itSid = (it as any).signalwireCallSid
            || (it as any).signalwire_call_sid
            || (it as any).callSid;
          return itSid === sid && !it.recordingUrl
            ? { ...it, recordingUrl }
            : it;
        }),
      );

      setSelectedHistoryCall((prev) => {
        if (!prev) return prev;
        const prevSid = (prev as any).signalwireCallSid
          || (prev as any).signalwire_call_sid
          || (prev as any).callSid;
        if (prevSid === sid && !prev.recordingUrl) {
          return { ...prev, recordingUrl };
        }
        return prev;
      });
    };

    const handleRecording = (data: { call_sid?: string; recording_url?: string }) => {
      if (data.call_sid && data.recording_url) {
        patchInteraction(data.call_sid, data.recording_url);
      }
    };

    // call_update fires when ANY call field changes, including recording_url
    // (we added an emit_call_update in /api/webhooks/recording). Same patch
    // logic — pull the URL out of the payload if present.
    const handleCallUpdate = (data: { call: any }) => {
      const call = data?.call;
      if (!call) return;
      const sid = call.signalwireCallSid || call.signalwire_call_sid || call.call_sid;
      const url = call.recordingUrl || call.recording_url;
      if (sid && url) patchInteraction(sid, url);
    };

    socket.on('recording', handleRecording);
    socket.on('call_update', handleCallUpdate);

    return () => {
      socket.off('recording', handleRecording);
      socket.off('call_update', handleCallUpdate);
    };
  }, [socket]);

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
      .catch(err => logger.error('Failed to fetch AI agents:', err));
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
      logger.error('Failed to delete contact:', error);
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
      logger.error('Failed to load interactions:', error);
    } finally {
      setIsLoadingInteractions(false);
    }
  };

  const [dialError, setDialError] = useState<string | null>(null);

  const handleCall = async () => {
    setDialError(null);

    // If not online, need to go online first (for inbound handling)
    if (!isOnline) {
      logger.debug('📞 [ContactDetail] Going online first...');
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

      logger.debug('📞 [ContactDetail] Initiating call to:', contact.phone);
      setIsAICall(false);
      setTranscription([]);
      const result = await makeCall(contact.phone, context);
      logger.debug('📞 [ContactDetail] Call initiated:', result);
    } catch (error: any) {
      logger.error('❌ [ContactDetail] Failed to initiate call:', error);
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
      logger.error('Failed to send AI agent:', error);
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
          logger.warn('📞 [ContactDetail] hangup() failed (may already be disconnected):', err);
        }
      }

      // Always end the call via backend API so the phone call is terminated
      // and the frontend gets proper socket events to clean up
      const callId = activeCallForContact?.id
        || activeCallForContact?.signalwire_call_sid
        || (activeCallForContact as any)?.signalwireCallSid;
      const callSid = effectiveCallSid || callId;

      if (callSid) {
        logger.debug('📞 [ContactDetail] Ending call via API:', callSid);
        await api.post(`/api/calls/${callSid}/end`);
      }
    } catch (error) {
      logger.error('Failed to end call:', error);
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
      logger.error('No call SID available for takeover');
      return;
    }

    // Go online if not already
    if (!isOnline) {
      await goOnline();
    }

    try {
      logger.debug('📞 [TakeOver] Initiating takeover for call:', callSid);

      // Call the takeover API to get the resource dial address (includes token)
      const response = await api.post(`/api/calls/${callSid}/takeover`);
      const { dial_address, leg_id } = response.data;

      logger.debug('📞 [TakeOver] Got dial address:', dial_address);

      // Dial the resource address — token is embedded in the URL
      // The SWML will use execute_rpc to end AI + connect to call:{sid}
      await makeCallToSwml(dial_address, {
        contact_id: contact.id,
        original_call_sid: callSid,
        leg_id: leg_id
      });

      // Update state - no longer an AI call once we've taken over
      setIsAICall(false);
      logger.debug('📞 [TakeOver] Successfully initiated takeover');

    } catch (error: any) {
      logger.error('Failed to take over call:', error);
      const errorMessage = error.response?.data?.error || 'Failed to take over call';
      logger.error('Error details:', errorMessage);
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
      {/* Header — contact identity on page bg (Restraint rs-main). No raised
          band: the KPI strip below is the only bordered card. */}
      <div className="px-6 pt-5 pb-4">
        {/* chead — avatar · name/phone · inline actions (rs-chead) */}
        <div className="flex items-center gap-3.5">
          {/* Avatar — 46px rounded square monogram (rs-bigav) */}
          <div className="w-[46px] h-[46px] rounded-xl bg-canvas-elevated border border-rule-strong flex items-center justify-center text-ink text-[17px] font-semibold flex-shrink-0">
            {contact.displayName.split(' ').map((w) => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase() || '?'}
          </div>

          {/* Name + phone */}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-[23px] font-semibold text-ink tracking-tight leading-tight truncate">
                {contact.displayName}
              </h2>
              {contact.isVip && (
                <Star className="w-4 h-4 text-status-warning fill-status-warning flex-shrink-0" />
              )}
              {contact.isBlocked && (
                <Chip dot="error"><Ban className="w-2.5 h-2.5" />Blocked</Chip>
              )}
            </div>
            <div className="mono text-[11.5px] text-ink-dim mt-[3px]">{contact.phone}</div>
          </div>

          {/* rs-actions — inline, right-aligned */}
          <div className="ml-auto flex items-center gap-2">
            {isAgentOnCall ? (
              <>
                {/* Live duration + cost pill (green=connected, amber=ringing, ✦=AI) */}
                <PillBadge
                  ai={showAIControls}
                  dot={showAIControls ? undefined : callState === 'ringing' ? 'warning' : 'success'}
                  time={
                    isOutboundCallInProgress && callState !== 'active'
                      ? getOutboundCallStatus()
                      : formatCallDuration(activeCallForContact?.duration || callDuration)
                  }
                  cost={
                    costRates && callState === 'active'
                      ? `~$${(
                          ((activeCallForContact?.duration || callDuration) / 60) *
                          (showAIControls
                            ? (costRates.ai_runtime_per_min || 0) + (costRates.voice_inbound_per_min || 0)
                            : (costRates.voice_inbound_per_min || 0) +
                              (costRates.webrtc_per_min || 0) +
                              2 * (costRates.conference_per_participant_min || 0))
                        ).toFixed(2)}`
                      : undefined
                  }
                />
                {showAIControls && <Chip ai>AI Agent</Chip>}
                {!showAIControls && activeCall && (
                  <Button
                    variant="secondary"
                    onClick={() => (isMuted ? unmute() : mute())}
                    icon={isMuted ? <MicOff className="w-3.5 h-3.5" /> : <Mic className="w-3.5 h-3.5" />}
                  >
                    {isMuted ? 'Unmute' : 'Mute'}
                  </Button>
                )}
                {showAIControls && (
                  <Button
                    variant="primary"
                    onClick={handleTakeOver}
                    disabled={isInitializing}
                    icon={<Phone className="w-3.5 h-3.5" />}
                  >
                    Take over
                  </Button>
                )}
                <Button variant="danger" onClick={handleEndCall} icon={<PhoneOff className="w-3.5 h-3.5" />}>
                  End call
                </Button>
              </>
            ) : (
              <>
                <Button
                  variant="primary"
                  onClick={handleCall}
                  disabled={isInitializing}
                  icon={<Phone className="w-3.5 h-3.5" />}
                >
                  {isInitializing ? 'Connecting…' : 'Call'}
                </Button>
                <Button variant="secondary" onClick={handleSendAI} icon={<Bot className="w-3.5 h-3.5" />}>
                  Send AI agent
                </Button>
                {contact.email && (
                  <Button
                    variant="secondary"
                    onClick={() => window.open(`mailto:${contact.email}`)}
                    icon={<Mail className="w-3.5 h-3.5" />}
                  >
                    Email
                  </Button>
                )}
              </>
            )}

            {/* ⋯ overflow — Edit + Delete */}
            <div className="relative" ref={moreMenuRef}>
              <Button
                variant="secondary"
                iconOnly
                onClick={() => setShowMoreMenu(!showMoreMenu)}
                aria-label="More actions"
              >
                <MoreHorizontal className="w-4 h-4" />
              </Button>
              {showMoreMenu && (
                <div className="absolute right-0 mt-1 w-48 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
                  <button
                    onClick={() => { setShowMoreMenu(false); setIsEditing(true); }}
                    className="w-full flex items-center gap-2 px-3 py-2 text-ink hover:bg-canvas-hover transition-colors text-[13px]"
                  >
                    <Edit2 className="w-4 h-4" />
                    Edit contact
                  </button>
                  <button
                    onClick={handleDeleteContact}
                    disabled={isDeleting}
                    className="w-full flex items-center gap-2 px-3 py-2 text-status-error hover:bg-canvas-hover transition-colors disabled:opacity-50 text-[13px]"
                  >
                    <Trash2 className="w-4 h-4" />
                    {isDeleting ? 'Deleting…' : 'Delete contact'}
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {dialError && (
          <div className="flex items-center gap-2 px-3 py-1 mt-2.5 w-fit bg-status-error-soft border border-status-error/30 rounded text-status-error text-[12px]">
            <span>{dialError}</span>
            <button onClick={() => setDialError(null)} className="ml-1 hover:text-ink">✕</button>
          </div>
        )}

        {/* Active-call secondary controls — feature-rich panel + observer
            (Listen) + conference participant list. Wiring kept verbatim; only
            the surrounding container is restyled into a secondary row. */}
        {isAgentOnCall && (
          <>
            <div className="flex items-center gap-2 mt-3 flex-wrap">
              <CallControlPanel
                callId={activeCallForContact?.id || currentCallSid || ''}
                callSid={effectiveCallSid || ''}
                isAICall={showAIControls}
                isHumanCall={!showAIControls}
                isInConference={isInConference}
                isOnHold={isOnHold}
                isRecording={isRecording}
                userRole={currentUser?.role}
                call={activeCallForContact}
                onHoldChange={setIsOnHold}
                onRecordingChange={setIsRecording}
              />
              {(activeCallForContact?.id || currentCallSid)
                && !panel.showHumanControls
                && activeCallForContact?.assigned_agent_id !== currentUser?.id
                && callCaps.canMonitor && (
                <ObserverControls
                  callId={(activeCallForContact?.id ?? currentCallSid) as string | number}
                  callType={showAIControls ? 'ai' : 'human'}
                />
              )}
            </div>
            {isInConference && callCaps.isMultiPartyCapable
              && conferenceParticipants.length > 0 && (
              <ConferenceParticipants
                participants={conferenceParticipants}
                className="mt-3"
              />
            )}
          </>
        )}

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

        {/* Pending callback banner (Tier 2r) */}
        <PendingCallbackBanner contactId={contact.id} />

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
                <span className="kicker text-ai">Dispatch AI agent</span>
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
                    <input type="checkbox" className="w-3.5 h-3.5 rounded-sm accent-sw-fuchsia"
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
            <div className="flex flex-wrap gap-1.5">
              {contact.tags.map((tag, index) => (
                <Chip key={index}>{tag}</Chip>
              ))}
            </div>
          </div>
        )}

        {/* KPI strip — bordered card, 5 equal divider-separated cells (Restraint).
            HeroStat internals (kicker + value + tone logic) are preserved inside
            each cell; tone-aware sentiment coloring (live/urgent/default) is kept. */}
        {hasAnyActiveCall ? (
          /* During a live/AI call the KPIs collapse to a quiet one-line statline
             (rc-statline) so the live transcript / coach surface dominates. */
          <div className="mt-[18px] flex items-center gap-6 flex-wrap text-[12px] text-ink-dim">
            <span>Total calls <b className="mono font-medium text-ink ml-0.5">{contact.totalCalls}</b></span>
            <span>
              Avg sentiment{' '}
              <b className={`mono font-medium ml-0.5 ${
                contact.averageSentiment != null
                  ? contact.averageSentiment > 0.3 ? 'text-status-success'
                    : contact.averageSentiment < -0.3 ? 'text-status-error' : 'text-ink'
                  : 'text-ink'
              }`}>
                {contact.averageSentiment != null ? (contact.averageSentiment > 0 ? '+' : '') + contact.averageSentiment.toFixed(1) : '—'}
              </b>
            </span>
            <span>Tier <b className="font-medium text-ink capitalize ml-0.5">{contact.accountTier}</b></span>
            <span>Last contact <b className="mono font-medium text-ink ml-0.5">{formatDate(contact.lastInteractionAt)}</b></span>
          </div>
        ) : (
          <div className="mt-[18px] grid grid-cols-5 border border-rule rounded-lg bg-canvas-raised overflow-hidden">
            <div className="px-4 py-3 border-r border-rule">
              <HeroStat kicker="Total calls" value={String(contact.totalCalls)} />
            </div>
            <div className="px-4 py-3 border-r border-rule">
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
            </div>
            <div className="px-4 py-3 border-r border-rule">
              <HeroStat kicker="Tier" value={contact.accountTier} isTier />
            </div>
            <div className="px-4 py-3 border-r border-rule">
              <HeroStat
                kicker="Open tickets"
                value={String(
                  (contact as any).openTickets
                    ?? contact.customFields?.openTickets
                    ?? contact.customFields?.open_tickets
                    ?? '—',
                )}
              />
            </div>
            <div className="px-4 py-3">
              <HeroStat kicker="Last contact" value={formatDate(contact.lastInteractionAt)} isSmall />
            </div>
          </div>
        )}
      </div>

      {/* Tabs — Restraint underline subtabs (active = fuchsia underline). Live
          and Call Detail render conditionally; AI-handled live call carries the
          turquoise ✦ signal in its label rather than a colored underline. */}
      <div className="px-5 pt-1">
        <Tabs
          value={activeTab}
          onChange={(v) => setActiveTab(v)}
          tabs={[
            ...(hasAnyActiveCall
              ? [{
                  value: 'live' as const,
                  label: (
                    <span className={`inline-flex items-center gap-1.5 ${showAIControls ? 'text-ai' : ''}`}>
                      {showAIControls ? (
                        <><span aria-hidden>{AI_GLYPH}</span>AI Call</>
                      ) : isOutboundCallInProgress && callState !== 'active' ? (
                        getOutboundCallStatus()
                      ) : (
                        <><StatusDot status="success" size="chip" />Live Call</>
                      )}
                    </span>
                  ),
                } as TabItem<typeof activeTab>]
              : []),
            { value: 'history' as const, label: 'Call History' },
            ...(selectedHistoryCall
              ? [{
                  value: 'callDetail' as const,
                  label: 'Call Detail',
                  onClose: handleCloseCallDetail,
                } as TabItem<typeof activeTab>]
              : []),
            { value: 'notes' as const, label: 'Notes' },
            { value: 'details' as const, label: 'Details' },
          ]}
        />
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-y-auto">
        {activeTab === 'live' && hasAnyActiveCall && (
          <LiveCallTab
            transcription={transcription}
            isAICall={showAIControls}
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
  // Restraint rs-kpis: sentence-case 11px label over a calm mono 17px value;
  // color only marks sentiment deviation (success/error), Tier reads as a word.
  void isSmall;
  const color = tone === 'live' ? 'text-status-success' : tone === 'urgent' ? 'text-status-error' : 'text-ink';
  return (
    <div className="flex flex-col gap-[3px]">
      <span className="text-[11px] font-medium text-ink-dim">{kicker}</span>
      {isTier ? (
        <span className={`text-[17px] font-semibold capitalize leading-none ${color}`}>{value}</span>
      ) : (
        <span className={`mono text-[17px] font-medium leading-none tabular-nums ${color}`}>{value}</span>
      )}
    </div>
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

  // Derive from/to handler chips (rs-hist's "✦ Receptionist → sofia@acme.com").
  // Maps the call's legs (or single handlerType) onto the CallHistoryRow's
  // from/to slots — AI legs carry the turquoise ✦ signal.
  const handlerRefs = (interaction: Interaction): { from?: { label: string; ai?: boolean }; to?: { label: string; ai?: boolean } } => {
    const legs = interaction.legs;
    const mk = (leg: CallLeg) => {
      const isAI = leg.legType === 'ai_agent';
      return { label: isAI ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent'), ai: isAI };
    };
    if (legs && legs.length > 0) {
      if (legs.length === 1) return { from: mk(legs[0]) };
      return { from: mk(legs[0]), to: mk(legs[legs.length - 1]) };
    }
    if (interaction.handlerType === 'ai') return { from: { label: interaction.aiAgentName || 'AI', ai: true } };
    return {};
  };

  const outcomeFor = (status?: string): { label: string; status: RestraintStatus } | undefined => {
    if (!status) return undefined;
    const dot: RestraintStatus =
      status === 'completed' ? 'success' :
      status === 'failed' || status === 'abandoned' ? 'error' :
      status === 'missed' || status === 'no_answer' ? 'warning' :
      'neutral';
    const label = status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ');
    return { label, status: dot };
  };

  return (
    <div className="px-6 pt-1">
      {interactions.map((interaction) => {
        const { from, to } = handlerRefs(interaction);
        const sentimentChip =
          interaction.sentimentScore == null ? undefined : {
            label: `${interaction.sentimentScore > 0 ? '+' : ''}${interaction.sentimentScore.toFixed(1)}`,
            dot: (interaction.sentimentScore > 0.3 ? 'success' :
                  interaction.sentimentScore < -0.3 ? 'error' : 'neutral') as RestraintStatus,
          };
        const rawCost = (interaction as any).estimatedCost;
        const cost = rawCost != null
          ? `$${Number(rawCost) < 0.1 ? Number(rawCost).toFixed(3) : Number(rawCost).toFixed(2)}`
          : undefined;
        return (
          <CallHistoryRow
            key={interaction.id}
            direction={interaction.direction === 'inbound' ? 'inbound' : 'outbound'}
            from={from}
            to={to}
            outcome={outcomeFor(interaction.status)}
            extraChips={sentimentChip ? [sentimentChip] : undefined}
            date={formatDate(interaction.createdAt)}
            duration={interaction.duration ? formatDuration(interaction.duration) : undefined}
            metaExtra={cost}
            summary={interaction.summary ? <AISummaryDisplay summary={interaction.summary} /> : undefined}
            onClick={() => onSelectCall(interaction)}
          />
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
      logger.error('Failed to save notes:', error);
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
      logger.error('Failed to update contact:', error);
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
              <input type="checkbox" className="w-3.5 h-3.5 rounded-sm accent-sw-fuchsia"
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
