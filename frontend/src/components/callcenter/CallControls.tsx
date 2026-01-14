import React from 'react';
import { motion } from 'framer-motion';
import {
  Phone,
  PhoneOff,
  PhoneForwarded,
  Pause,
  Play,
  Mic,
  MicOff,
  Users,
  Volume2,
  Settings
} from 'lucide-react';
import { cn } from '../../lib/utils';

interface CallControlsProps {
  isOnHold: boolean;
  isMuted: boolean;
  onHold: () => void;
  onMute: () => void;
  onTransfer: () => void;
  onEndCall: () => void;
  onConference?: () => void;
}

interface ControlButtonProps {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  variant?: 'default' | 'danger' | 'warning' | 'success';
  active?: boolean;
  disabled?: boolean;
  shortcut?: string;
}

const ControlButton: React.FC<ControlButtonProps> = ({
  icon,
  label,
  onClick,
  variant = 'default',
  active = false,
  disabled = false,
  shortcut
}) => {
  const variants = {
    default: active
      ? 'bg-blue-100 text-blue-700 border-blue-300'
      : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50',
    danger: 'bg-red-100 text-red-700 border-red-300 hover:bg-red-200',
    warning: active
      ? 'bg-yellow-100 text-yellow-700 border-yellow-300'
      : 'bg-white text-yellow-600 border-yellow-300 hover:bg-yellow-50',
    success: 'bg-green-100 text-green-700 border-green-300 hover:bg-green-200'
  };

  return (
    <motion.button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex flex-col items-center justify-center px-4 py-3 rounded-lg border-2 transition-all relative',
        variants[variant],
        disabled && 'opacity-50 cursor-not-allowed'
      )}
      whileHover={!disabled ? { scale: 1.05 } : {}}
      whileTap={!disabled ? { scale: 0.95 } : {}}
      title={shortcut ? `${label} (${shortcut})` : label}
    >
      {shortcut && (
        <span className="absolute top-1 right-1 text-[10px] font-mono bg-gray-200 px-1 rounded">
          {shortcut}
        </span>
      )}
      <div className="mb-1">{icon}</div>
      <span className="text-xs font-medium">{label}</span>
    </motion.button>
  );
};

export const CallControls: React.FC<CallControlsProps> = ({
  isOnHold,
  isMuted,
  onHold,
  onMute,
  onTransfer,
  onEndCall,
  onConference
}) => {
  return (
    <div className="border-t border-gray-200 bg-gray-50 p-4">
      <div className="flex items-center justify-center space-x-3">
        {/* Mute/Unmute */}
        <ControlButton
          icon={isMuted ? <MicOff className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
          label={isMuted ? 'Unmute' : 'Mute'}
          onClick={onMute}
          variant={isMuted ? 'warning' : 'default'}
          active={isMuted}
          shortcut="M"
        />

        {/* Hold/Resume */}
        <ControlButton
          icon={isOnHold ? <Play className="w-5 h-5" /> : <Pause className="w-5 h-5" />}
          label={isOnHold ? 'Resume' : 'Hold'}
          onClick={onHold}
          variant={isOnHold ? 'warning' : 'default'}
          active={isOnHold}
          shortcut="H"
        />

        {/* Transfer */}
        <ControlButton
          icon={<PhoneForwarded className="w-5 h-5" />}
          label="Transfer"
          onClick={onTransfer}
          variant="default"
          shortcut="T"
        />

        {/* Conference (optional) */}
        {onConference && (
          <ControlButton
            icon={<Users className="w-5 h-5" />}
            label="Conference"
            onClick={onConference}
            variant="default"
          />
        )}

        {/* Volume/Settings */}
        <ControlButton
          icon={<Volume2 className="w-5 h-5" />}
          label="Volume"
          onClick={() => console.log('Volume settings')}
          variant="default"
        />

        {/* Divider */}
        <div className="w-px h-12 bg-gray-300 mx-2" />

        {/* End Call */}
        <ControlButton
          icon={<PhoneOff className="w-5 h-5" />}
          label="End Call"
          onClick={onEndCall}
          variant="danger"
          shortcut="⇧ESC"
        />
      </div>

      {/* Keyboard Shortcuts Hint */}
      <div className="mt-3 text-center">
        <p className="text-xs text-gray-500">
          <kbd className="px-1.5 py-0.5 bg-gray-200 rounded text-xs">M</kbd> Mute
          <span className="mx-2">•</span>
          <kbd className="px-1.5 py-0.5 bg-gray-200 rounded text-xs">H</kbd> Hold
          <span className="mx-2">•</span>
          <kbd className="px-1.5 py-0.5 bg-gray-200 rounded text-xs">T</kbd> Transfer
          <span className="mx-2">•</span>
          <kbd className="px-1.5 py-0.5 bg-gray-200 rounded text-xs">Shift+ESC</kbd> End Call
        </p>
      </div>
    </div>
  );
};