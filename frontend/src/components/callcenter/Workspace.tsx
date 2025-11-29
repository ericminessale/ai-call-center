import React from 'react';
import { motion } from 'framer-motion';
import { Phone, PhoneMissed, PhoneForwarded, Pause, MicOff } from 'lucide-react';
import { CallHeader } from './CallHeader';
import { TranscriptionArea } from './TranscriptionArea';
import { CallControls } from './CallControls';
import type { Call } from '../../types/callcenter';

interface WorkspaceProps {
  activeCall: Call | null;
  onHold: () => void;
  onTransfer: () => void;
  onEndCall: () => void;
}

export const Workspace: React.FC<WorkspaceProps> = ({
  activeCall,
  onHold,
  onTransfer,
  onEndCall
}) => {
  if (!activeCall) {
    return (
      <div className="h-full flex items-center justify-center">
        <motion.div
          className="text-center"
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.3 }}
        >
          <div className="w-20 h-20 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
            <Phone className="w-10 h-10 text-gray-400" />
          </div>
          <h2 className="text-xl font-medium text-gray-700 mb-2">No Active Call</h2>
          <p className="text-gray-500">
            Waiting for incoming call or select from queue
          </p>
        </motion.div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Call Header */}
      <CallHeader call={activeCall} />

      {/* Main Call Area */}
      <div className="flex-1 overflow-hidden bg-white m-4 rounded-lg shadow-sm border border-gray-200">
        <div className="h-full flex flex-col">
          {/* Status Bar */}
          {activeCall.isOnHold && (
            <motion.div
              className="bg-yellow-50 border-b border-yellow-200 px-4 py-2"
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
            >
              <div className="flex items-center justify-center text-yellow-700">
                <Pause className="w-4 h-4 mr-2" />
                <span className="text-sm font-medium">Call is on hold</span>
              </div>
            </motion.div>
          )}

          {/* Transcription Area */}
          <TranscriptionArea
            messages={activeCall.transcription || []}
            isOnHold={activeCall.isOnHold}
          />

          {/* Call Controls */}
          <CallControls
            isOnHold={activeCall.isOnHold || false}
            onHold={onHold}
            onTransfer={onTransfer}
            onEndCall={onEndCall}
            isMuted={false}
            onMute={() => console.log('Toggle mute')}
          />
        </div>
      </div>
    </div>
  );
};