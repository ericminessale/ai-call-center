import { useState, useEffect } from 'react';
import { Phone, PhoneOff, User, Bot, Star, Building2 } from 'lucide-react';
import { contactsApi } from '../../services/api';

interface IncomingCallBannerProps {
  phoneNumber: string;
  aiContext?: {
    agentName?: string;
    reason?: string;
    sentiment?: number;
  };
  onAnswer: () => void;
  onDecline: () => void;
}

interface ContactInfo {
  displayName: string;
  company?: string;
  isVip?: boolean;
  accountTier?: string;
}

export function IncomingCallBanner({
  phoneNumber,
  aiContext,
  onAnswer,
  onDecline,
}: IncomingCallBannerProps) {
  const [contactInfo, setContactInfo] = useState<ContactInfo | null>(null);
  const [isLookingUp, setIsLookingUp] = useState(true);

  // Lookup contact by phone number
  useEffect(() => {
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
      } catch (error) {
        // Contact not found - that's okay
        console.log('Contact not found for:', phoneNumber);
      } finally {
        setIsLookingUp(false);
      }
    };

    lookupContact();
  }, [phoneNumber]);

  const displayName = contactInfo?.displayName || 'Unknown Caller';
  const isKnown = !!contactInfo;
  const wasAI = !!aiContext?.agentName;

  // Format phone number for display
  const formatPhone = (phone: string) => {
    // Basic formatting - adjust as needed
    if (phone.length === 11 && phone.startsWith('1')) {
      return `+1 (${phone.slice(1, 4)}) ${phone.slice(4, 7)}-${phone.slice(7)}`;
    }
    return phone;
  };

  return (
    <div className="fixed top-0 left-0 right-0 z-50 animate-slide-down">
      <div className="bg-gradient-to-r from-green-600 to-green-700 shadow-lg">
        <div className="max-w-4xl mx-auto px-4 py-3">
          <div className="flex items-center justify-between">
            {/* Left - Caller Info */}
            <div className="flex items-center gap-4">
              {/* Pulsing phone icon */}
              <div className="relative">
                <div className="w-12 h-12 bg-white/20 rounded-full flex items-center justify-center">
                  <Phone className="w-6 h-6 text-white animate-pulse" />
                </div>
                <div className="absolute inset-0 rounded-full border-2 border-white/50 animate-ping" />
              </div>

              {/* Caller details */}
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-lg font-semibold text-white">
                    {isLookingUp ? 'Looking up...' : displayName}
                  </span>
                  {contactInfo?.isVip && (
                    <Star className="w-4 h-4 text-yellow-300 fill-yellow-300" />
                  )}
                  {!isKnown && !isLookingUp && (
                    <span className="px-2 py-0.5 bg-white/20 text-white text-xs rounded">
                      New
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-3 text-sm text-white/80">
                  <span>{formatPhone(phoneNumber)}</span>
                  {contactInfo?.company && (
                    <>
                      <span>•</span>
                      <span className="flex items-center gap-1">
                        <Building2 className="w-3 h-3" />
                        {contactInfo.company}
                      </span>
                    </>
                  )}
                  {contactInfo?.accountTier && contactInfo.accountTier !== 'prospect' && (
                    <>
                      <span>•</span>
                      <span className="capitalize">{contactInfo.accountTier}</span>
                    </>
                  )}
                </div>

                {/* AI Context (if escalated from AI) */}
                {wasAI && (
                  <div className="flex items-center gap-2 mt-1 text-xs text-white/70">
                    <Bot className="w-3 h-3" />
                    <span>Transferred from {aiContext.agentName}</span>
                    {aiContext.reason && (
                      <>
                        <span>•</span>
                        <span>{aiContext.reason}</span>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Right - Action Buttons */}
            <div className="flex items-center gap-3">
              <button
                onClick={onDecline}
                className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors shadow-lg"
              >
                <PhoneOff className="w-4 h-4" />
                <span className="font-medium">Decline</span>
              </button>

              <button
                onClick={onAnswer}
                className="flex items-center gap-2 px-6 py-2 bg-white hover:bg-gray-100 text-green-700 rounded-lg transition-colors shadow-lg"
              >
                <Phone className="w-4 h-4" />
                <span className="font-medium">Answer</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default IncomingCallBanner;
