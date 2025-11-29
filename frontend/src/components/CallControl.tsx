import { useState, useEffect } from 'react';
import { Phone, Loader2, AlertCircle } from 'lucide-react';
import { callsApi } from '../services/api';
import websocket from '../services/websocket';
import toast from 'react-hot-toast';
import { Call } from '../types';

interface CallControlProps {
  onCallStart?: (call: Call) => void;
  onCallEnd?: () => void;
}

export default function CallControl({ onCallStart, onCallEnd }: CallControlProps) {
  const [destination, setDestination] = useState('');
  const [destinationType, setDestinationType] = useState<'phone' | 'sip'>('phone');
  const [isLoading, setIsLoading] = useState(false);
  const [validationError, setValidationError] = useState<string>('');

  // Phone number validation (E.164 format)
  const validatePhoneNumber = (number: string): boolean => {
    // E.164 format: + followed by 1-15 digits
    const phoneRegex = /^\+[1-9]\d{1,14}$/;
    return phoneRegex.test(number);
  };

  // SIP URI validation
  const validateSipUri = (uri: string): boolean => {
    // More flexible SIP URI validation that handles:
    // - URL encoded characters (like %2F for /)
    // - Port numbers
    // - Transport parameters (;transport=tls)
    // - Any other SIP parameters
    // Examples: sip:user@domain, sip:user@domain:port, sip:Brian%2F@hq.sw.work:1061;transport=tls
    const sipRegex = /^sip:[^@\s]+@[^@\s]+(:[0-9]+)?(;[^;\s]+)*$/i;
    return sipRegex.test(uri);
  };

  // Auto-detect destination type and validate
  const handleDestinationChange = (value: string) => {
    setDestination(value);
    setValidationError('');

    if (!value) {
      return;
    }

    // Auto-detect type
    if (value.startsWith('+')) {
      setDestinationType('phone');
      if (value.length > 1 && !validatePhoneNumber(value)) {
        setValidationError('Invalid phone number format. Use E.164 format (e.g., +12125551234)');
      }
    } else if (value.toLowerCase().startsWith('sip:')) {
      setDestinationType('sip');
      if (value.length > 4 && !validateSipUri(value)) {
        setValidationError('Invalid SIP URI format. Use format: sip:user@domain.com');
      }
    } else {
      // If it doesn't start with + or sip:, show guidance
      if (value.length > 0) {
        setValidationError('Start with "+" for phone numbers or "sip:" for SIP URIs');
      }
    }
  };

  useEffect(() => {
    // Listen for call ended event to reset the form
    const handleCallEnded = (data: any) => {
      if (data.reset_ui) {
        setDestination(''); // Reset destination field
        setValidationError(''); // Clear validation errors
      }
    };

    websocket.on('call_ended', handleCallEnded);

    return () => {
      websocket.off('call_ended', handleCallEnded);
    };
  }, []);

  const handleInitiateCall = async () => {
    if (!destination) {
      toast.error('Please enter a destination');
      return;
    }

    // Final validation before making the call
    if (destinationType === 'phone' && !validatePhoneNumber(destination)) {
      toast.error('Please enter a valid phone number in E.164 format');
      return;
    }

    if (destinationType === 'sip' && !validateSipUri(destination)) {
      toast.error('Please enter a valid SIP URI');
      return;
    }

    setIsLoading(true);
    try {
      const response = await callsApi.initiate(destination, destinationType, true); // Always enable transcription
      const newCall = {
        id: response.data.call_id,
        signalwire_call_sid: response.data.call_sid,
        destination: response.data.destination,
        destination_type: destinationType,
        status: response.data.status,
        transcription_active: true,
        user_id: '',
        created_at: new Date().toISOString(),
      };

      // Notify parent component about the new call
      if (onCallStart) {
        onCallStart(newCall);
      }

      // Reset form after successful call initiation
      setDestination('');
      setValidationError('');

      // Join the SignalWire call ID room for real-time updates (this is the event channel)
      // The call_id from SignalWire is the actual channel name for events
      const token = localStorage.getItem('access_token');
      if (token) {
        websocket.emit('join_call', {
          call_sid: response.data.call_id,
          token: token
        });
      }
    } catch (error: any) {
      toast.error(error.response?.data?.error || 'Failed to initiate call');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      <h2 className="text-2xl font-bold mb-6">Call Control Panel</h2>

      <div className="space-y-4">
          <div>
            <label htmlFor="destination" className="block text-sm font-medium text-gray-700 mb-1">
              Destination
            </label>
            <div className="relative">
              <input
                id="destination"
                type="text"
                value={destination}
                onChange={(e) => handleDestinationChange(e.target.value)}
                placeholder="+12125551234 or sip:user@domain.com"
                className={`w-full px-3 py-2 pr-10 border rounded-md focus:outline-none focus:ring-2 ${
                  validationError
                    ? 'border-red-300 focus:ring-red-500'
                    : 'border-gray-300 focus:ring-blue-500'
                }`}
              />
              {destination && !validationError && (
                <div className="absolute inset-y-0 right-0 flex items-center pr-3">
                  <span className={`text-xs font-medium px-2 py-1 rounded-full ${
                    destinationType === 'phone'
                      ? 'bg-blue-100 text-blue-700'
                      : 'bg-purple-100 text-purple-700'
                  }`}>
                    {destinationType === 'phone' ? 'Phone' : 'SIP'}
                  </span>
                </div>
              )}
            </div>
            {validationError && (
              <div className="mt-1 flex items-start">
                <AlertCircle className="h-4 w-4 text-red-500 mt-0.5 mr-1 flex-shrink-0" />
                <span className="text-sm text-red-600">{validationError}</span>
              </div>
            )}
            {!destination && (
              <p className="mt-1 text-xs text-gray-500">
                Enter a phone number starting with + or a SIP URI starting with sip:
              </p>
            )}
          </div>

          <button
            onClick={handleInitiateCall}
            disabled={isLoading || !destination || !!validationError}
            className="w-full flex items-center justify-center px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? (
              <>
                <Loader2 className="animate-spin h-5 w-5 mr-2" />
                Initiating...
              </>
            ) : (
              <>
                <Phone className="h-5 w-5 mr-2" />
                Start Call
              </>
            )}
          </button>
        </div>
    </div>
  );
}