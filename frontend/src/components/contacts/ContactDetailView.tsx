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
} from 'lucide-react';
import { Contact, Interaction, TranscriptionMessage, Call, CallLeg } from '../../types/callcenter';
import { contactsApi } from '../../services/api';
import api from '../../services/api';
import { useCallFabric } from '../../hooks/useCallFabric';
import { useSocketContext } from '../../contexts/SocketContext';
import { CallTimeline } from './CallTimeline';

interface ContactDetailViewProps {
  contact: Contact;
  onContactUpdate: (contact: Contact) => void;
  onContactDelete?: (contactId: number) => void;
  activeCallForContact?: Call; // Inbound/AI call for this contact from parent
}

export function ContactDetailView({ contact, onContactUpdate, onContactDelete, activeCallForContact }: ContactDetailViewProps) {
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
    makeCallToSwml,
    hangup,
    goOnline,
    mute,
    unmute,
  } = useCallFabric();

  // Determine if there's an outbound call in progress (even if activeCall isn't set yet)
  // callState is set to 'ringing' immediately when makeCall() is called
  const isOutboundCallInProgress = callState === 'ringing' || callState === 'active' || callState === 'ending';

  // Any active call: browser outbound, AI outbound, or inbound from parent
  const hasAnyActiveCall = !!(activeCall || currentCallSid || activeCallForContact || isOutboundCallInProgress);

  // Get display status for outbound calls
  const getOutboundCallStatus = () => {
    if (callState === 'ringing' && !activeCall) return 'Calling...';
    if (callState === 'ringing') return 'Ringing...';
    if (callState === 'active') return 'Connected';
    if (callState === 'ending') return 'Ending...';
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

  // Listen for transcription updates via WebSocket
  useEffect(() => {
    if (!effectiveCallSid || !socket) return;

    console.log('📝 [ContactDetail] Subscribing to transcription for call:', effectiveCallSid);

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
        setTranscription(prev => [...prev, {
          id: `${Date.now()}`,
          speaker: speaker || 'caller',
          text: data.text,
          timestamp: new Date().toISOString(),
        }]);
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

  const handleCall = async () => {
    if (!isOnline) {
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

      setIsAICall(false);
      setTranscription([]);
      await makeCall(contact.phone, context);
    } catch (error) {
      console.error('Failed to initiate call:', error);
    }
  };

  const handleSendAI = async () => {
    try {
      // Call backend to initiate outbound AI call
      const response = await api.post('/api/ai/outbound-call', {
        phone: contact.phone,
        contact_id: contact.id,
        agent_type: 'sales', // or let user choose
        context: {
          contact_name: contact.displayName,
          account_tier: contact.accountTier,
          is_vip: contact.isVip,
          company: contact.company,
          total_calls: contact.totalCalls,
          notes: contact.notes,
        }
      });

      if (response.data.success) {
        setIsAICall(true);
        setCurrentCallSid(response.data.call_sid);
        setTranscription([]);
      }
    } catch (error) {
      console.error('Failed to send AI agent:', error);
    }
  };

  const handleEndCall = async () => {
    try {
      // End AI calls via API, browser calls via Call Fabric
      if (isAICall || isInboundAICall) {
        const callSid = effectiveCallSid;
        if (callSid) {
          await api.post(`/api/calls/${callSid}/end`);
        }
      } else if (activeCall) {
        await hangup();
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

      // Call the takeover API to get the SWML URL
      const response = await api.post(`/api/calls/${callSid}/takeover`);
      const { swml_url, leg_id } = response.data;

      console.log('📞 [TakeOver] Got SWML URL:', swml_url);

      // Dial the SWML URL to bridge into the call
      await makeCallToSwml(swml_url, {
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
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="bg-gray-800 border-b border-gray-700 p-4">
        <div className="flex items-start gap-4">
          {/* Avatar */}
          <div className="w-16 h-16 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-2xl font-bold">
            {contact.displayName.charAt(0).toUpperCase()}
          </div>

          {/* Basic Info */}
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-semibold text-white">{contact.displayName}</h2>
              {contact.isVip && (
                <Star className="w-5 h-5 text-yellow-400 fill-yellow-400" />
              )}
              {contact.isBlocked && (
                <Ban className="w-5 h-5 text-red-500" />
              )}
            </div>
            <div className="flex items-center gap-4 mt-1 text-sm text-gray-400">
              {contact.company && (
                <span className="flex items-center gap-1">
                  <Building2 className="w-4 h-4" />
                  {contact.company}
                  {contact.jobTitle && ` - ${contact.jobTitle}`}
                </span>
              )}
            </div>
            <div className="flex items-center gap-4 mt-1 text-sm text-gray-400">
              <span className="flex items-center gap-1">
                <Phone className="w-4 h-4" />
                {contact.phone}
              </span>
              {contact.email && (
                <span className="flex items-center gap-1">
                  <Mail className="w-4 h-4" />
                  {contact.email}
                </span>
              )}
            </div>
          </div>

          {/* Edit Button */}
          <button
            onClick={() => setIsEditing(true)}
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
          >
            <Edit2 className="w-5 h-5" />
          </button>
        </div>

        {/* Action Buttons / Call Controls */}
        <div className="flex items-center gap-2 mt-4">
          {hasAnyActiveCall ? (
            // Active call controls (browser outbound, AI outbound, or inbound)
            <>
              <div className={`flex items-center gap-3 px-4 py-2 rounded-lg border ${
                isAICall || isInboundAICall
                  ? 'bg-purple-600/20 border-purple-500'
                  : callState === 'ringing'
                  ? 'bg-yellow-600/20 border-yellow-500'
                  : 'bg-green-600/20 border-green-500'
              }`}>
                <div className={`w-2 h-2 rounded-full animate-pulse ${
                  isAICall || isInboundAICall ? 'bg-purple-500' : callState === 'ringing' ? 'bg-yellow-500' : 'bg-green-500'
                }`} />
                <span className={`font-medium ${
                  isAICall || isInboundAICall ? 'text-purple-400' : callState === 'ringing' ? 'text-yellow-400' : 'text-green-400'
                }`}>
                  {/* Show call state for outbound calls, duration when connected */}
                  {isOutboundCallInProgress && callState !== 'active'
                    ? getOutboundCallStatus()
                    : formatCallDuration(activeCallForContact?.duration || callDuration)
                  }
                </span>
                {(isAICall || isInboundAICall) && (
                  <span className="flex items-center gap-1 px-2 py-0.5 bg-purple-500/30 text-purple-300 text-xs rounded-full">
                    <Bot className="w-3 h-3" />
                    AI Agent
                  </span>
                )}
              </div>

              {/* Mute button only for human browser calls */}
              {!isAICall && !isInboundAICall && activeCall && (
                <button
                  onClick={() => isMuted ? unmute() : mute()}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg transition-colors ${
                    isMuted ? 'bg-yellow-600 hover:bg-yellow-700' : 'bg-gray-700 hover:bg-gray-600'
                  } text-white`}
                >
                  {isMuted ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
                  {isMuted ? 'Unmute' : 'Mute'}
                </button>
              )}

              {/* Take Over button for AI calls */}
              {(isAICall || isInboundAICall) && (
                <button
                  onClick={handleTakeOver}
                  disabled={isInitializing}
                  className="flex items-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white rounded-lg transition-colors"
                >
                  <Phone className="w-4 h-4" />
                  Take Over
                </button>
              )}

              <button
                onClick={handleEndCall}
                className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors"
              >
                <PhoneOff className="w-4 h-4" />
                End Call
              </button>
            </>
          ) : (
            // Idle state - show call buttons
            <>
              <button
                onClick={handleCall}
                disabled={isInitializing}
                className="flex items-center gap-2 px-4 py-2 bg-green-600 hover:bg-green-700 disabled:bg-gray-600 text-white rounded-lg transition-colors"
              >
                <Phone className="w-4 h-4" />
                {isInitializing ? 'Connecting...' : 'Call'}
              </button>
              <button
                onClick={handleSendAI}
                className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg transition-colors"
              >
                <Bot className="w-4 h-4" />
                Send AI Agent
              </button>
              {contact.email && (
                <button
                  onClick={() => window.open(`mailto:${contact.email}`)}
                  className="flex items-center gap-2 px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors"
                >
                  <Mail className="w-4 h-4" />
                  Email
                </button>
              )}
            </>
          )}
          {/* More Menu */}
          <div className="relative ml-auto" ref={moreMenuRef}>
            <button
              onClick={() => setShowMoreMenu(!showMoreMenu)}
              className="p-2 text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
            >
              <MoreHorizontal className="w-5 h-5" />
            </button>
            {showMoreMenu && (
              <div className="absolute right-0 mt-1 w-48 bg-gray-800 border border-gray-700 rounded-lg shadow-lg z-50">
                <button
                  onClick={handleDeleteContact}
                  disabled={isDeleting}
                  className="w-full flex items-center gap-2 px-4 py-2 text-red-400 hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-50"
                >
                  <Trash2 className="w-4 h-4" />
                  {isDeleting ? 'Deleting...' : 'Delete Contact'}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Call Error */}
        {callError && (
          <div className="mt-2 p-2 bg-red-500/20 border border-red-500 rounded-lg text-red-400 text-sm flex items-center gap-2">
            <AlertCircle className="w-4 h-4" />
            {callError}
          </div>
        )}

        {/* Tags */}
        {contact.tags && contact.tags.length > 0 && (
          <div className="flex items-center gap-2 mt-4">
            <Tag className="w-4 h-4 text-gray-400" />
            <div className="flex flex-wrap gap-1">
              {contact.tags.map((tag, index) => (
                <span
                  key={index}
                  className="px-2 py-0.5 bg-gray-700 text-gray-300 text-xs rounded-full"
                >
                  {tag}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Quick Stats */}
        <div className="grid grid-cols-4 gap-4 mt-4 pt-4 border-t border-gray-700">
          <div className="text-center">
            <div className="text-2xl font-bold text-white">{contact.totalCalls}</div>
            <div className="text-xs text-gray-400">Total Calls</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-white">
              {contact.averageSentiment != null ?
                (contact.averageSentiment > 0 ? '+' : '') + contact.averageSentiment.toFixed(1) : '--'}
            </div>
            <div className="text-xs text-gray-400">Avg Sentiment</div>
          </div>
          <div className="text-center">
            <div className="text-lg font-bold text-white capitalize">
              {contact.accountTier}
            </div>
            <div className="text-xs text-gray-400">Account Tier</div>
          </div>
          <div className="text-center">
            <div className="text-lg font-bold text-white">
              {formatDate(contact.lastInteractionAt)}
            </div>
            <div className="text-xs text-gray-400">Last Contact</div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-gray-700 bg-gray-800">
        {/* Live Call tab - shown during any active call (browser outbound, AI, or inbound) */}
        {hasAnyActiveCall && (
          <button
            onClick={() => setActiveTab('live' as any)}
            className={`px-4 py-3 text-sm font-medium transition-colors ${
              activeTab === 'live'
                ? (isInboundAICall || isAICall ? 'text-purple-400 border-b-2 border-purple-400' : callState === 'ringing' ? 'text-yellow-400 border-b-2 border-yellow-400' : 'text-green-400 border-b-2 border-green-400')
                : (isInboundAICall || isAICall ? 'text-purple-400/70 hover:text-purple-400' : callState === 'ringing' ? 'text-yellow-400/70 hover:text-yellow-400' : 'text-green-400/70 hover:text-green-400')
            }`}
          >
            <div className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full animate-pulse ${isInboundAICall || isAICall ? 'bg-purple-500' : callState === 'ringing' ? 'bg-yellow-500' : 'bg-green-500'}`} />
              {isInboundAICall || isAICall ? (
                <>
                  <Bot className="w-4 h-4" />
                  AI Call
                </>
              ) : isOutboundCallInProgress && callState !== 'active' ? (
                <>
                  <PhoneOutgoing className="w-4 h-4" />
                  {getOutboundCallStatus()}
                </>
              ) : (
                'Live Call'
              )}
            </div>
          </button>
        )}
        <button
          onClick={() => setActiveTab('history')}
          className={`px-4 py-3 text-sm font-medium transition-colors ${
            activeTab === 'history'
              ? 'text-blue-400 border-b-2 border-blue-400'
              : 'text-gray-400 hover:text-white'
          }`}
        >
          <Clock className="w-4 h-4 inline-block mr-2" />
          Call History
        </button>
        {/* Call Detail tab - only shown when a historical call is selected */}
        {selectedHistoryCall && (
          <div
            onClick={() => setActiveTab('callDetail')}
            className={`px-4 py-3 text-sm font-medium transition-colors flex items-center gap-2 cursor-pointer ${
              activeTab === 'callDetail'
                ? 'text-orange-400 border-b-2 border-orange-400'
                : 'text-gray-400 hover:text-white'
            }`}
          >
            <FileText className="w-4 h-4" />
            Call Detail
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleCloseCallDetail();
              }}
              className="ml-1 p-0.5 hover:bg-gray-700 rounded"
              title="Close"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        <button
          onClick={() => setActiveTab('notes')}
          className={`px-4 py-3 text-sm font-medium transition-colors ${
            activeTab === 'notes'
              ? 'text-blue-400 border-b-2 border-blue-400'
              : 'text-gray-400 hover:text-white'
          }`}
        >
          <MessageSquare className="w-4 h-4 inline-block mr-2" />
          Notes
        </button>
        <button
          onClick={() => setActiveTab('details')}
          className={`px-4 py-3 text-sm font-medium transition-colors ${
            activeTab === 'details'
              ? 'text-blue-400 border-b-2 border-blue-400'
              : 'text-gray-400 hover:text-white'
          }`}
        >
          <User className="w-4 h-4 inline-block mr-2" />
          Details
        </button>
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
      <div className="p-8 text-center text-gray-400">
        <div className="animate-spin w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full mx-auto mb-2" />
        Loading history...
      </div>
    );
  }

  if (interactions.length === 0) {
    return (
      <div className="p-8 text-center text-gray-400">
        <Clock className="w-12 h-12 mx-auto mb-2 opacity-50" />
        <p>No call history yet</p>
      </div>
    );
  }

  // Helper to render handler chain from legs
  const renderHandlerChain = (interaction: Interaction) => {
    const legs = interaction.legs;
    if (!legs || legs.length === 0) {
      // Fallback to single handler display
      if (interaction.handlerType === 'ai') {
        return (
          <span className="flex items-center gap-1 px-2 py-0.5 bg-purple-500/20 text-purple-400 text-xs rounded-full">
            <Bot className="w-3 h-3" />
            {interaction.aiAgentName || 'AI'}
          </span>
        );
      }
      return null;
    }

    // Multiple legs - show chain
    if (legs.length === 1) {
      const leg = legs[0];
      return (
        <span className={`flex items-center gap-1 px-2 py-0.5 text-xs rounded-full ${
          leg.legType === 'ai_agent' ? 'bg-purple-500/20 text-purple-400' : 'bg-green-500/20 text-green-400'
        }`}>
          {leg.legType === 'ai_agent' ? <Bot className="w-3 h-3" /> : <User className="w-3 h-3" />}
          {leg.legType === 'ai_agent' ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}
        </span>
      );
    }

    // Multiple handlers - show chain
    return (
      <div className="flex items-center gap-1">
        {legs.map((leg, idx) => (
          <div key={leg.id} className="flex items-center">
            <span className={`flex items-center gap-0.5 px-1.5 py-0.5 text-xs rounded ${
              leg.legType === 'ai_agent' ? 'bg-purple-500/20 text-purple-400' : 'bg-green-500/20 text-green-400'
            }`}>
              {leg.legType === 'ai_agent' ? <Bot className="w-3 h-3" /> : <User className="w-3 h-3" />}
              <span className="hidden sm:inline">
                {leg.legType === 'ai_agent' ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}
              </span>
            </span>
            {idx < legs.length - 1 && (
              <span className="text-gray-500 mx-0.5">→</span>
            )}
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="divide-y divide-gray-700">
      {interactions.map((interaction) => (
        <div
          key={interaction.id}
          className="p-4 hover:bg-gray-800/50 transition-colors cursor-pointer"
          onClick={() => onSelectCall(interaction)}
        >
          <div className="flex items-start gap-3">
            {/* Direction Icon */}
            <div className={`w-10 h-10 rounded-full flex items-center justify-center ${
              interaction.direction === 'inbound' ? 'bg-blue-500/20' : 'bg-green-500/20'
            }`}>
              {interaction.direction === 'inbound' ? (
                <PhoneIncoming className={`w-5 h-5 ${
                  interaction.direction === 'inbound' ? 'text-blue-400' : 'text-green-400'
                }`} />
              ) : (
                <PhoneOutgoing className="w-5 h-5 text-green-400" />
              )}
            </div>

            {/* Call Info */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium text-white">
                  {interaction.direction === 'inbound' ? 'Inbound' : 'Outbound'} Call
                </span>
                {/* Handler chain or single handler badge */}
                {renderHandlerChain(interaction)}
                <span className={`px-2 py-0.5 text-xs rounded-full ${
                  interaction.status === 'completed' ? 'bg-green-500/20 text-green-400' :
                  interaction.status === 'active' || interaction.status === 'ai_active' ? 'bg-blue-500/20 text-blue-400' :
                  'bg-gray-500/20 text-gray-400'
                }`}>
                  {interaction.status}
                </span>
              </div>

              <div className="flex items-center gap-4 mt-1 text-sm text-gray-400">
                <span>{formatDate(interaction.createdAt)}</span>
                {interaction.duration && (
                  <span className="flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatDuration(interaction.duration)}
                  </span>
                )}
                {interaction.transcriptionActive && (
                  <span className="flex items-center gap-1 text-green-400">
                    <Mic className="w-3 h-3" />
                    Transcribed
                  </span>
                )}
                {/* Show handler count if multiple */}
                {interaction.legs && interaction.legs.length > 1 && (
                  <span className="flex items-center gap-1 text-orange-400">
                    <User className="w-3 h-3" />
                    {interaction.legs.length} handlers
                  </span>
                )}
              </div>

              {/* AI Summary */}
              {interaction.summary && (
                <div className="mt-2 p-2 bg-gray-700/50 rounded-lg text-sm text-gray-300">
                  <FileText className="w-4 h-4 inline-block mr-1 text-gray-400" />
                  {interaction.summary}
                </div>
              )}
            </div>

            {/* Sentiment indicator */}
            {interaction.sentimentScore != null && (
              <div className={`text-sm font-medium ${
                interaction.sentimentScore > 0.3 ? 'text-green-400' :
                interaction.sentimentScore < -0.3 ? 'text-red-400' :
                'text-gray-400'
              }`}>
                {interaction.sentimentScore > 0 ? '+' : ''}{interaction.sentimentScore.toFixed(1)}
              </div>
            )}
          </div>
        </div>
      ))}
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
    <div className="p-4">
      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="Add notes about this contact..."
        className="w-full h-48 p-3 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 resize-none focus:outline-none focus:border-blue-500"
      />
      <div className="flex justify-end mt-2">
        <button
          onClick={handleSave}
          disabled={isSaving || notes === (contact.notes || '')}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
        >
          {isSaving ? 'Saving...' : 'Save Notes'}
        </button>
      </div>
    </div>
  );
}

function DetailsTab({ contact }: { contact: Contact }) {
  return (
    <div className="p-4 space-y-4">
      <DetailRow label="First Name" value={contact.firstName} />
      <DetailRow label="Last Name" value={contact.lastName} />
      <DetailRow label="Display Name" value={contact.displayName} />
      <DetailRow label="Phone" value={contact.phone} />
      <DetailRow label="Email" value={contact.email} />
      <DetailRow label="Company" value={contact.company} />
      <DetailRow label="Job Title" value={contact.jobTitle} />
      <DetailRow label="Account Tier" value={contact.accountTier} />
      <DetailRow label="Account Status" value={contact.accountStatus} />
      <DetailRow label="External ID" value={contact.externalId} />
      <DetailRow label="VIP" value={contact.isVip ? 'Yes' : 'No'} />
      <DetailRow label="Blocked" value={contact.isBlocked ? 'Yes' : 'No'} />
      <DetailRow label="Created" value={new Date(contact.createdAt).toLocaleString()} />
      <DetailRow label="Updated" value={new Date(contact.updatedAt).toLocaleString()} />

      {contact.customFields && Object.keys(contact.customFields).length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-semibold text-gray-400 uppercase mb-2">Custom Fields</h3>
          {Object.entries(contact.customFields).map(([key, value]) => (
            <DetailRow key={key} label={key} value={String(value)} />
          ))}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value?: string }) {
  return (
    <div className="flex items-center py-2 border-b border-gray-700">
      <span className="w-32 text-sm text-gray-400">{label}</span>
      <span className="text-white">{value || '--'}</span>
    </div>
  );
}

function LiveCallTab({
  transcription,
  isAICall,
  callSid,
  callDuration,
  callState,
  isOutboundCallInProgress,
}: {
  transcription: TranscriptionMessage[];
  isAICall: boolean;
  callSid?: string;
  callDuration?: number;
  callState?: 'idle' | 'ringing' | 'active' | 'ending';
  isOutboundCallInProgress?: boolean;
}) {
  const [systemMessage, setSystemMessage] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Debug logging for props
  useEffect(() => {
    console.log('🎯 [LiveCallTab] Component rendered with props:', { isAICall, callSid, transcriptionCount: transcription.length });
  }, [isAICall, callSid, transcription.length]);

  // Auto-scroll to bottom when new transcription arrives
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcription]);

  const quickTemplates = [
    { label: 'Offer Discount', message: 'The customer qualifies for a 20% discount. Offer this to help close the sale.' },
    { label: 'Transfer to Human', message: 'This customer needs specialized help. Transfer them to a human agent now.' },
    { label: 'Apologize', message: 'Acknowledge the customer\'s frustration with empathy and apologize for any inconvenience.' },
    { label: 'Gather Details', message: 'Ask more specific questions to better understand the customer\'s needs.' },
  ];

  const sendSystemMessage = async () => {
    console.log('🎯 [AI MESSAGE] Send button clicked');
    console.log('🎯 [AI MESSAGE] callSid:', callSid);
    console.log('🎯 [AI MESSAGE] systemMessage:', systemMessage);
    console.log('🎯 [AI MESSAGE] isAICall:', isAICall);

    if (!systemMessage.trim()) {
      console.log('🎯 [AI MESSAGE] No message to send');
      return;
    }
    if (!callSid) {
      console.error('🎯 [AI MESSAGE] No call SID available!');
      setError('No active call SID available');
      return;
    }

    setIsSending(true);
    setError(null);
    setSuccess(false);

    try {
      const payload = {
        call_id: callSid,
        message: systemMessage,
        role: 'system'
      };
      console.log('🎯 [AI MESSAGE] Sending payload:', payload);

      const response = await api.post('/api/ai/inject-message', payload);
      console.log('🎯 [AI MESSAGE] Response:', response);

      setSuccess(true);
      setSystemMessage('');
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: any) {
      console.error('🎯 [AI MESSAGE] Failed to send AI message:', err);
      console.error('🎯 [AI MESSAGE] Error response:', err.response?.data);
      setError(err.response?.data?.error || 'Failed to send message to AI agent');
    } finally {
      setIsSending(false);
    }
  };

  // Get status display
  const getStatusDisplay = () => {
    if (isOutboundCallInProgress) {
      if (callState === 'ringing') return { text: 'Calling...', color: 'text-yellow-400', bgColor: 'bg-yellow-500' };
      if (callState === 'ending') return { text: 'Ending...', color: 'text-gray-400', bgColor: 'bg-gray-500' };
    }
    if (callState === 'active') return { text: 'Connected', color: 'text-green-400', bgColor: 'bg-green-500' };
    return { text: 'Recording', color: 'text-green-400', bgColor: 'bg-green-500' };
  };

  const status = getStatusDisplay();

  return (
    <div className="h-full flex flex-col">
      {/* Live Transcription */}
      <div className="flex-1 p-4 overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-white">
            {isOutboundCallInProgress && callState === 'ringing' ? 'Outbound Call' : 'Live Transcription'}
          </h3>
          <div className={`flex items-center gap-2 ${status.color} text-sm`}>
            <div className={`w-2 h-2 ${status.bgColor} rounded-full animate-pulse`} />
            {status.text}
          </div>
        </div>

        <div className="bg-gray-900 rounded-lg p-4 min-h-[300px] max-h-[400px] overflow-y-auto font-mono text-sm">
          {/* Show calling state UI when outbound call is ringing */}
          {isOutboundCallInProgress && callState === 'ringing' && transcription.length === 0 ? (
            <div className="flex items-center justify-center h-64 text-gray-500">
              <div className="text-center">
                <PhoneOutgoing className="w-12 h-12 mx-auto mb-2 text-yellow-400 animate-pulse" />
                <p className="text-yellow-400 font-medium">Calling...</p>
                <p className="text-gray-500 text-sm mt-1">Waiting for answer</p>
              </div>
            </div>
          ) : transcription.length > 0 ? (
            <div className="space-y-3">
              {transcription.map((entry, idx) => (
                <div key={entry.id || idx} className="flex flex-col space-y-1">
                  <div className="flex items-center space-x-2">
                    <span className={`font-semibold ${
                      entry.speaker === 'agent' || entry.speaker === 'ai' ? 'text-purple-400' : 'text-blue-400'
                    }`}>
                      {entry.speaker === 'agent' ? 'Agent:' : entry.speaker === 'ai' ? 'AI:' : 'Caller:'}
                    </span>
                    <span className="text-xs text-gray-500">
                      {new Date(entry.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <p className="text-gray-300 pl-4">{entry.text}</p>
                </div>
              ))}
              <div ref={scrollRef} />
            </div>
          ) : (
            <div className="flex items-center justify-center h-64 text-gray-500">
              <div className="text-center">
                <Mic className="w-12 h-12 mx-auto mb-2 opacity-50" />
                <p>Waiting for conversation...</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* AI Message Controls - Only for AI calls */}
      {isAICall && (
        <div className="border-t border-gray-700 p-4 bg-gray-800">
          <div className="bg-purple-900/30 border border-purple-500/30 rounded-lg p-3 mb-3">
            <div className="flex items-start gap-2">
              <Bot className="w-4 h-4 text-purple-400 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-purple-300">
                Send instructions to guide the AI agent's behavior during this call
              </p>
            </div>
          </div>

          {/* Quick Templates */}
          <div className="flex flex-wrap gap-2 mb-3">
            {quickTemplates.map((template, idx) => (
              <button
                key={idx}
                onClick={() => setSystemMessage(template.message)}
                className="text-xs px-3 py-1.5 bg-purple-500/20 text-purple-300 rounded-md hover:bg-purple-500/30 transition-colors"
                disabled={isSending}
              >
                {template.label}
              </button>
            ))}
          </div>

          {/* Message Input */}
          <div className="flex gap-2">
            <input
              type="text"
              value={systemMessage}
              onChange={(e) => setSystemMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  console.log('🎯 [AI MESSAGE] Enter key pressed');
                  sendSystemMessage();
                }
              }}
              placeholder="Type message to AI agent..."
              className="flex-1 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-400 focus:outline-none focus:border-purple-500"
              disabled={isSending}
            />
            <button
              onClick={() => {
                console.log('🎯 [AI MESSAGE] Button clicked directly');
                sendSystemMessage();
              }}
              disabled={!systemMessage.trim() || isSending}
              className={`px-4 py-2 rounded-lg font-medium transition-colors flex items-center gap-2 ${
                systemMessage.trim() && !isSending
                  ? 'bg-purple-600 text-white hover:bg-purple-700'
                  : 'bg-gray-600 text-gray-400 cursor-not-allowed'
              }`}
            >
              {isSending ? (
                <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent" />
              ) : (
                <Send className="w-4 h-4" />
              )}
              Send
            </button>
          </div>

          {/* Status Messages */}
          {error && (
            <div className="mt-2 p-2 bg-red-500/20 border border-red-500/50 rounded-lg text-xs text-red-400 flex items-center gap-2">
              <AlertCircle className="w-3 h-3 flex-shrink-0" />
              {error}
            </div>
          )}
          {success && (
            <div className="mt-2 p-2 bg-green-500/20 border border-green-500/50 rounded-lg text-xs text-green-400 flex items-center gap-2">
              <Bot className="w-3 h-3 flex-shrink-0" />
              Message sent to AI agent successfully!
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Call Detail Tab - displays historical call details with transcription
function CallDetailTab({
  interaction,
  formatDate,
  formatDuration,
}: {
  interaction: Interaction;
  formatDate: (date?: string) => string;
  formatDuration: (seconds?: number) => string;
}) {
  const [transcriptions, setTranscriptions] = useState<{ speaker: string; text: string; timestamp: string }[]>([]);
  const [legs, setLegs] = useState<CallLeg[]>([]);
  const [isLoadingTranscriptions, setIsLoadingTranscriptions] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Fetch transcriptions and legs for this call
  useEffect(() => {
    const fetchCallDetails = async () => {
      setIsLoadingTranscriptions(true);
      try {
        // Use the SignalWire call SID to fetch call details which includes transcriptions
        const response = await api.get(`/api/calls/${interaction.signalwireCallSid}`);
        const data = response.data.transcriptions || [];
        setTranscriptions(data.map((t: any) => ({
          speaker: t.speaker || 'caller',
          text: t.transcript || t.text,
          timestamp: t.createdAt || t.created_at,
        })));

        // Fetch legs separately
        try {
          const legsResponse = await api.get(`/api/calls/${interaction.signalwireCallSid}/legs`);
          setLegs(legsResponse.data.legs || []);
        } catch (legsError) {
          console.log('No legs data available for this call');
          setLegs([]);
        }
      } catch (error) {
        console.error('Failed to load call details:', error);
        setTranscriptions([]);
        setLegs([]);
      } finally {
        setIsLoadingTranscriptions(false);
      }
    };

    fetchCallDetails();
  }, [interaction.signalwireCallSid]);

  return (
    <div className="h-full flex flex-col">
      {/* Call Info Header */}
      <div className="p-4 border-b border-gray-700 bg-gray-800/50">
        <div className="flex items-center gap-3 mb-3">
          <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
            interaction.direction === 'inbound' ? 'bg-blue-500/20' : 'bg-green-500/20'
          }`}>
            {interaction.direction === 'inbound' ? (
              <PhoneIncoming className="w-6 h-6 text-blue-400" />
            ) : (
              <PhoneOutgoing className="w-6 h-6 text-green-400" />
            )}
          </div>
          <div>
            <h3 className="text-lg font-semibold text-white">
              {interaction.direction === 'inbound' ? 'Inbound' : 'Outbound'} Call
            </h3>
            <p className="text-sm text-gray-400">
              {formatDate(interaction.createdAt)} • {formatDuration(interaction.duration)}
            </p>
          </div>
          {interaction.handlerType === 'ai' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-purple-500/20 text-purple-400 text-sm rounded-full ml-auto">
              <Bot className="w-4 h-4" />
              {interaction.aiAgentName || 'AI Agent'}
            </span>
          )}
        </div>

        {/* Call Details Grid */}
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-gray-500">From:</span>
            <span className="text-white ml-2">{interaction.fromNumber || '--'}</span>
          </div>
          <div>
            <span className="text-gray-500">To:</span>
            <span className="text-white ml-2">{interaction.destination || '--'}</span>
          </div>
          <div>
            <span className="text-gray-500">Status:</span>
            <span className="text-white ml-2 capitalize">{interaction.status}</span>
          </div>
          <div>
            <span className="text-gray-500">Handler:</span>
            <span className="text-white ml-2 capitalize">{interaction.handlerType}</span>
          </div>
        </div>

        {/* Summary if available */}
        {interaction.summary && (
          <div className="mt-4 p-3 bg-gray-900 rounded-lg">
            <h4 className="text-sm font-medium text-gray-300 mb-1">AI Summary</h4>
            <p className="text-sm text-gray-400">{interaction.summary}</p>
          </div>
        )}

        {/* Call Journey Timeline */}
        {legs.length > 0 && (
          <CallTimeline legs={legs} />
        )}
      </div>

      {/* Transcription Section */}
      <div className="flex-1 p-4 overflow-y-auto">
        <h4 className="text-sm font-semibold text-white mb-3">Call Transcription</h4>

        {isLoadingTranscriptions ? (
          <div className="flex items-center justify-center h-32 text-gray-400">
            <div className="animate-spin w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full mr-2" />
            Loading transcription...
          </div>
        ) : transcriptions.length > 0 ? (
          <div className="bg-gray-900 rounded-lg p-4 space-y-3 font-mono text-sm">
            {transcriptions.map((entry, idx) => (
              <div key={idx} className="flex flex-col space-y-1">
                <div className="flex items-center space-x-2">
                  <span className={`font-semibold ${
                    entry.speaker === 'agent' || entry.speaker === 'ai' ? 'text-purple-400' : 'text-blue-400'
                  }`}>
                    {entry.speaker === 'agent' ? 'Agent:' : entry.speaker === 'ai' ? 'AI:' : 'Caller:'}
                  </span>
                  {entry.timestamp && (
                    <span className="text-xs text-gray-500">
                      {new Date(entry.timestamp).toLocaleTimeString()}
                    </span>
                  )}
                </div>
                <p className="text-gray-300 pl-4">{entry.text}</p>
              </div>
            ))}
            <div ref={scrollRef} />
          </div>
        ) : (
          <div className="bg-gray-900 rounded-lg p-8 text-center text-gray-500">
            <Mic className="w-10 h-10 mx-auto mb-2 opacity-50" />
            <p>No transcription available for this call</p>
          </div>
        )}
      </div>

      {/* Recording link if available */}
      {interaction.recordingUrl && (
        <div className="p-4 border-t border-gray-700">
          <a
            href={interaction.recordingUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 text-blue-400 hover:text-blue-300 text-sm"
          >
            <Play className="w-4 h-4" />
            Listen to Recording
          </a>
        </div>
      )}
    </div>
  );
}

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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-gray-800 rounded-lg p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold text-white mb-4">Edit Contact</h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">First Name</label>
              <input
                type="text"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Last Name</label>
              <input
                type="text"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Display Name *</label>
            <input
              type="text"
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })}
              required
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Phone *</label>
            <input
              type="tel"
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
              required
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Email</label>
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">Company</label>
              <input
                type="text"
                value={formData.company}
                onChange={(e) => setFormData({ ...formData, company: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Job Title</label>
              <input
                type="text"
                value={formData.jobTitle}
                onChange={(e) => setFormData({ ...formData, jobTitle: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Account Tier</label>
            <select
              value={formData.accountTier}
              onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500"
            >
              <option value="prospect">Prospect</option>
              <option value="free">Free</option>
              <option value="pro">Pro</option>
              <option value="enterprise">Enterprise</option>
            </select>
          </div>

          <div className="flex items-center gap-6">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={formData.isVip}
                onChange={(e) => setFormData({ ...formData, isVip: e.target.checked })}
                className="w-4 h-4 rounded bg-gray-700 border-gray-600"
              />
              <span className="text-white">VIP Customer</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={formData.isBlocked}
                onChange={(e) => setFormData({ ...formData, isBlocked: e.target.checked })}
                className="w-4 h-4 rounded bg-gray-700 border-gray-600"
              />
              <span className="text-white">Blocked</span>
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white rounded-lg transition-colors"
            >
              {isSaving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default ContactDetailView;
