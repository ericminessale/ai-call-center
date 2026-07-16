import { useState, useEffect } from 'react';
import { Phone, PhoneOff, Star, Building2, UserPlus, Shield } from 'lucide-react';
import { contactsApi } from '../../services/api';
import { AgentContextCard, AgentCollectedData, hasContext } from '../shared/AgentContextCard';

interface IncomingCallBannerProps {
  phoneNumber: string;
  callerName?: string;
  queueId?: string;
  aiContext?: AgentCollectedData;
  onAnswer: () => void;
  onDecline: () => void;
  assignmentType?: 'normal' | 'backup' | 'escalation';
  requestingAgent?: { id: number; name: string; email: string };
  whisperMode?: boolean;
}

interface ContactInfo {
  displayName: string;
  company?: string;
  isVip?: boolean;
  accountTier?: string;
}

export function IncomingCallBanner({
  phoneNumber,
  callerName,
  queueId,
  aiContext,
  onAnswer,
  onDecline,
  assignmentType = 'normal',
  requestingAgent,
  whisperMode,
}: IncomingCallBannerProps) {
  const [contactInfo, setContactInfo] = useState<ContactInfo | null>(null);
  const [isLookingUp, setIsLookingUp] = useState(!callerName);

  useEffect(() => {
    if (callerName) {
      setIsLookingUp(false);
      return;
    }
    const lookupContact = async () => {
      try {
        const response = await contactsApi.lookup(phoneNumber);
        const data = response.data;
        if (data && 'displayName' in data) {
          setContactInfo({
            displayName: data.displayName,
            company: data.company,
            isVip: data.isVip,
            accountTier: data.accountTier,
          });
        }
      } catch {
        // Contact not found - that's okay
      } finally {
        setIsLookingUp(false);
      }
    };
    lookupContact();
  }, [phoneNumber, callerName]);

  const displayName = callerName || contactInfo?.displayName || 'Unknown caller';
  const isKnown = !!contactInfo || !!callerName;
  const wasAI = hasContext(aiContext);
  const isFromQueue = !!queueId;
  const isBackup = assignmentType === 'backup';
  const isEscalation = assignmentType === 'escalation';
  const isMultiAgent = isBackup || isEscalation;

  // Left rail color — single thin vertical accent, no full-width tint background
  const railColor = isEscalation
    ? 'bg-wait'
    : isBackup
      ? 'bg-sw-turquoise'
      : 'bg-live';

  const answerLabel = isEscalation
    ? (whisperMode ? 'Join · whisper' : 'Join call')
    : isBackup
    ? 'Join as backup'
    : 'Answer';

  const formatPhone = (phone: string) => {
    if (phone.length === 11 && phone.startsWith('1')) {
      return `+1 (${phone.slice(1, 4)}) ${phone.slice(4, 7)}-${phone.slice(7)}`;
    }
    return phone;
  };

  return (
    // Renders inline at the top of the page flex container (UnifiedAgentDesktop
    // mounts it before the header). Was previously `fixed top-0 left-0 right-0`
    // which overlaid the header + tabs — agents couldn't navigate while a call
    // was assigned. Inline-flow placement pushes header/content down naturally.
    <div className="relative w-full z-40 animate-slide-down">
      <div className="relative bg-canvas-raised border-b border-rule shadow-md">
        {/* Thin colored left rail — single accent, no full-width tint */}
        <div className={`absolute left-0 top-0 bottom-0 w-[3px] ${railColor}`} />

        <div className="max-w-5xl mx-auto px-6 py-3.5">
          <div className="flex items-center justify-between gap-4">
            {/* Left — Caller */}
            <div className="flex items-center gap-4 min-w-0">
              {/* Pulse dot instead of icon box — less visual weight */}
              <div className="relative flex items-center justify-center w-3 h-3">
                <span className={`absolute inset-0 rounded-full ${railColor} animate-pulse`} />
                <span className={`absolute inset-0 rounded-full ${railColor} opacity-40 animate-ping`} />
              </div>

              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="kicker">
                    {isEscalation ? 'Escalation' : isBackup ? 'Backup requested' : 'Incoming call'}
                  </span>
                  {isMultiAgent && requestingAgent && (
                    <span className="text-[11px] text-ink-muted">
                      · from <span className="text-ink">{requestingAgent.name}</span>
                      {whisperMode && <span className="ml-1">· whisper</span>}
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-2 mt-0.5">
                  <span className="font-heading font-semibold text-[20px] text-ink leading-none truncate">
                    {isLookingUp ? 'Looking up…' : displayName}
                  </span>
                  {contactInfo?.isVip && (
                    <Star className="w-4 h-4 text-wait fill-wait flex-shrink-0" />
                  )}
                  {!isKnown && !isLookingUp && (
                    <span className="text-[11px] text-ink-muted">· New</span>
                  )}
                </div>

                <div className="flex items-center gap-2 text-[12px] text-ink-muted mt-1">
                  <span className="mono">{formatPhone(phoneNumber)}</span>
                  {contactInfo?.company && (
                    <>
                      <span className="text-ink-faint">·</span>
                      <span className="flex items-center gap-1">
                        <Building2 className="w-3 h-3" />
                        {contactInfo.company}
                      </span>
                    </>
                  )}
                  {contactInfo?.accountTier && contactInfo.accountTier !== 'prospect' && (
                    <>
                      <span className="text-ink-faint">·</span>
                      <span className="capitalize">{contactInfo.accountTier}</span>
                    </>
                  )}
                  {isFromQueue && (
                    <>
                      <span className="text-ink-faint">·</span>
                      <span className="mono text-[11px]">{queueId}</span>
                    </>
                  )}
                </div>
              </div>
            </div>

            {/* Right — Actions. Solid fills, white text, no tints. */}
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={onDecline}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-sm bg-urgent hover:bg-urgent/90 text-white font-semibold text-[13px] transition-colors"
              >
                <PhoneOff className="w-3.5 h-3.5" />
                Decline
              </button>
              <button
                onClick={onAnswer}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-sm bg-live hover:bg-live/90 text-white font-semibold text-[13px] transition-colors"
              >
                {isEscalation ? <Shield className="w-3.5 h-3.5" /> :
                 isBackup ? <UserPlus className="w-3.5 h-3.5" /> :
                 <Phone className="w-3.5 h-3.5" />}
                {answerLabel}
              </button>
            </div>
          </div>

          {/* AI Triage Context */}
          {wasAI && aiContext && (
            <div className="mt-3">
              <AgentContextCard context={aiContext} variant="compact" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default IncomingCallBanner;
