import { Users, Mic, MicOff, Phone, Shield, Headphones, UserPlus } from 'lucide-react';
import { ConferenceParticipant } from '../../types/callcenter';

interface ConferenceParticipantsProps {
  participants: ConferenceParticipant[];
  className?: string;
}

const typeConfig: Record<string, { label: string; color: string; icon: typeof Users }> = {
  customer: { label: 'Customer', color: 'bg-blue-500/20 text-blue-400 border-blue-500/30', icon: Phone },
  agent: { label: 'Agent', color: 'bg-green-500/20 text-green-400 border-green-500/30', icon: Headphones },
  ai: { label: 'AI', color: 'bg-purple-500/20 text-purple-400 border-purple-500/30', icon: Users },
  supervisor: { label: 'Supervisor', color: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30', icon: Shield },
  backup: { label: 'Backup', color: 'bg-orange-500/20 text-orange-400 border-orange-500/30', icon: UserPlus },
};

export function ConferenceParticipants({ participants, className = '' }: ConferenceParticipantsProps) {
  const activeParticipants = participants.filter(p => p.status === 'active' || p.status === 'joining');

  if (activeParticipants.length === 0) return null;

  return (
    <div className={`bg-gray-800/50 border border-gray-700 rounded-lg p-3 ${className}`}>
      <div className="flex items-center gap-2 mb-2">
        <Users className="w-4 h-4 text-gray-400" />
        <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
          Conference ({activeParticipants.length})
        </span>
      </div>
      <div className="space-y-1.5">
        {activeParticipants.map((p) => {
          const config = typeConfig[p.participantType] || typeConfig.agent;
          const Icon = config.icon;
          return (
            <div
              key={p.id}
              className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-md bg-gray-900/50"
            >
              <div className="flex items-center gap-2 min-w-0">
                <Icon className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                <span className="text-sm text-gray-300 truncate">
                  {p.participantId || `Participant ${p.id}`}
                </span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${config.color}`}>
                  {config.label}
                </span>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                {p.status === 'joining' && (
                  <span className="text-[10px] text-yellow-400 animate-pulse">Joining...</span>
                )}
                {p.isMuted ? (
                  <MicOff className="w-3.5 h-3.5 text-red-400" title="Muted" />
                ) : (
                  <Mic className="w-3.5 h-3.5 text-green-400" title="Unmuted" />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
