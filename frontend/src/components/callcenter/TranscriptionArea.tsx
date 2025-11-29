import React, { useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Bot, User, Headphones, Sparkles } from 'lucide-react';
import { format } from 'date-fns';
import { cn } from '../../lib/utils';
import type { TranscriptionMessage, Sentiment } from '../../types/callcenter';

interface TranscriptionAreaProps {
  messages: TranscriptionMessage[];
  isOnHold?: boolean;
}

const SentimentIndicator: React.FC<{ value: Sentiment; size?: 'sm' | 'md' }> = ({
  value,
  size = 'sm'
}) => {
  const emojis = {
    very_negative: '😠',
    negative: '😟',
    neutral: '😐',
    positive: '😊',
    very_positive: '😄'
  };

  const colors = {
    very_negative: 'text-red-600',
    negative: 'text-orange-600',
    neutral: 'text-gray-600',
    positive: 'text-green-600',
    very_positive: 'text-emerald-600'
  };

  return (
    <div className={cn(
      'flex items-center space-x-1',
      size === 'sm' ? 'text-xs' : 'text-sm'
    )}>
      <span className={size === 'sm' ? 'text-base' : 'text-lg'}>
        {emojis[value]}
      </span>
      <span className={cn('font-medium', colors[value])}>
        {value.replace('_', ' ')}
      </span>
    </div>
  );
};

const TranscriptionMessage: React.FC<{
  message: TranscriptionMessage;
  isLastMessage: boolean;
}> = ({ message, isLastMessage }) => {
  const isAgent = message.speaker === 'agent';
  const isAI = message.speaker === 'ai';
  const isCustomer = message.speaker === 'customer';

  const getAvatar = () => {
    if (isAgent) return <Headphones className="w-4 h-4" />;
    if (isAI) return <Bot className="w-4 h-4" />;
    return <User className="w-4 h-4" />;
  };

  const getAvatarBg = () => {
    if (isAgent) return 'bg-blue-100 text-blue-600';
    if (isAI) return 'bg-gradient-to-br from-purple-100 to-indigo-100 text-indigo-600';
    return 'bg-gray-100 text-gray-600';
  };

  const getMessageBg = () => {
    if (isAgent) return 'bg-blue-50 border-blue-200';
    if (isAI) return 'bg-gradient-to-r from-purple-50 to-indigo-50 border-indigo-200';
    return 'bg-white border-gray-200';
  };

  return (
    <motion.div
      className={cn(
        'flex mb-4',
        isAgent ? 'justify-end' : 'justify-start'
      )}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
    >
      <div className={cn(
        'flex max-w-[70%]',
        isAgent ? 'flex-row-reverse' : 'flex-row'
      )}>
        {/* Avatar */}
        <div className={cn(
          'flex-shrink-0',
          isAgent ? 'ml-3' : 'mr-3'
        )}>
          <div className={cn(
            'w-8 h-8 rounded-full flex items-center justify-center',
            getAvatarBg()
          )}>
            {getAvatar()}
          </div>
        </div>

        {/* Message Content */}
        <div className="flex-1">
          {/* Speaker Name and Time */}
          <div className={cn(
            'flex items-center mb-1',
            isAgent ? 'justify-end' : 'justify-start'
          )}>
            <span className="text-xs font-medium text-gray-600">
              {message.speakerName || (isAgent ? 'Agent' : isAI ? 'AI Assistant' : 'Customer')}
            </span>
            {isAI && (
              <Sparkles className="w-3 h-3 text-indigo-500 ml-1" />
            )}
            <span className="text-xs text-gray-400 ml-2">
              {format(new Date(message.timestamp), 'HH:mm:ss')}
            </span>
          </div>

          {/* Message Bubble */}
          <div className={cn(
            'rounded-lg px-4 py-2 border shadow-sm',
            getMessageBg(),
            isAgent ? 'rounded-br-none' : 'rounded-bl-none'
          )}>
            <p className="text-sm text-gray-900 leading-relaxed">
              {message.text}
            </p>

            {/* Metadata */}
            {message.metadata && (
              <div className="mt-2 pt-2 border-t border-gray-200">
                <div className="text-xs text-gray-600">
                  {Object.entries(message.metadata).map(([key, value]) => (
                    <div key={key}>
                      <span className="font-medium">{key}:</span> {String(value)}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Sentiment for Customer Messages */}
            {isCustomer && message.sentiment && (
              <div className="mt-2 pt-2 border-t border-gray-200">
                <SentimentIndicator value={message.sentiment} size="sm" />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Typing Indicator for Last Message */}
      {isLastMessage && (
        <motion.div
          className="ml-11 mt-1"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3 }}
        >
          <div className="flex space-x-1">
            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" />
            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0.1s' }} />
            <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }} />
          </div>
        </motion.div>
      )}
    </motion.div>
  );
};

export const TranscriptionArea: React.FC<TranscriptionAreaProps> = ({
  messages,
  isOnHold = false
}) => {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div className={cn(
      'flex-1 overflow-y-auto p-4',
      isOnHold && 'opacity-50'
    )}>
      {messages.length === 0 ? (
        <div className="flex items-center justify-center h-full">
          <div className="text-center">
            <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-3">
              <Headphones className="w-8 h-8 text-gray-400" />
            </div>
            <p className="text-gray-500">Waiting for conversation to begin...</p>
          </div>
        </div>
      ) : (
        <AnimatePresence>
          {messages.map((message, index) => (
            <TranscriptionMessage
              key={message.id}
              message={message}
              isLastMessage={index === messages.length - 1}
            />
          ))}
        </AnimatePresence>
      )}
      <div ref={scrollRef} />
    </div>
  );
};