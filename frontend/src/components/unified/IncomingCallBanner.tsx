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
        if (response.data) {
          setContactInfo({
            displayName: response.data.displayName,
            company: response.data.company,
            isVip: response.data.isVip,
            accountTier: response.data.accountTier,
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

  // Tone sets the left rail color
  const toneClass = isEscalation
    ? 'border-l-wait bg-wait/5'
    : isBackup
      ? 'border-l-sw-blue bg-sw-blue/5'
      : 'border-l-live bg-live/5';

  const iconColor = isEscalation ? 'text-wait-soft' : isBackup ? 'text-sw-blue' : 'text-live-soft';

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
    <div className="fixed top-0 left-0 right-0 z-50 animate-slide-down">
      <div className={`relative ${toneClass} border-b border-rule border-l-[3px] shadow-panel`}>
        {/* Subtle scan accent */}
        <div className="absolute inset-x-0 top-0 h-px overflow-hidden pointer-events-none">
          <div className="scanline" />
        </div>

        <div className="max-w-5xl mx-auto px-5 py-3.5">
          <div className="flex items-center justify-between gap-4">
            {/* Left — Caller */}
            <div className="flex items-center gap-4 min-w-0">
              <div className="relative">
                <div className={`w-11 h-11 rounded flex items-center justify-center ${iconColor} bg-canvas-raised border border-current/30`}>
                  {isEscalation ? <Shield className="w-5 h-5" /> :
                   isBackup ? <UserPlus className="w-5 h-5" /> :
                   <Phone className="w-5 h-5" />}
                </div>
                <span className={`absolute -top-1 -right-1 w-3 h-3 rounded-full ${
                  isEscalation ? 'bg-wait' : isBackup ? 'bg-sw-blue' : 'bg-live'
                } shadow-[0_0_10px_currentColor] animate-pulse`} />
              </div>

              <div className="min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="kicker">
                    {isEscalation ? 'Escalation' : isBackup ? 'Backup requested' : 'Incoming call'}
                  </span>
                  {isMultiAgent && requestingAgent && (
                    <span className="text-[11px] text-ink-dim">
                      from <span className="text-ink">{requestingAgent.name}</span>
                      {whisperMode && <span className="ml-1 text-ink-dim">(whisper)</span>}
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <span className="font-display text-[22px] text-ink leading-none truncate">
                    {isLookingUp ? 'Looking up…' : displayName}
                  </span>
                  {contactInfo?.isVip && (
                    <Star className="w-4 h-4 text-wait fill-wait flex-shrink-0" />
                  )}
                  {!isKnown && !isLookingUp && (
                    <span className="chip chip-muted">New</span>
                  )}
                </div>

                <div className="flex items-center gap-2.5 text-[12px] text-ink-dim mt-1">
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
                      <span className="chip chip-muted">{queueId}</span>
                    </>
                  )}
                </div>
              </div>
            </div>

            {/* Right — Actions */}
            <div className="flex items-center gap-2 shrink-0">
              <button onClick={onDecline} className="btn-danger">
                <PhoneOff className="w-3.5 h-3.5" />
                Decline
              </button>
              <button onClick={onAnswer} className="btn-primary !py-2 !px-4">
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
