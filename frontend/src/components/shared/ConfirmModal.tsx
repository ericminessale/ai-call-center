import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';

interface ConfirmModalProps {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'warning';
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  title,
  message,
  confirmLabel = 'Delete',
  cancelLabel = 'Cancel',
  variant = 'danger',
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onCancel]);

  const iconTone =
    variant === 'danger'
      ? 'text-urgent-soft bg-urgent/15'
      : 'text-wait-soft bg-wait/15';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onCancel} />

      <div className="relative bg-canvas-raised rounded-md shadow-panel w-full max-w-sm">
        <div className="px-5 pt-5 pb-5 flex items-start gap-4">
          <div className={`p-2 rounded-full ${iconTone} flex-shrink-0`}>
            <AlertTriangle className="w-4 h-4" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="font-heading text-[16px] font-semibold text-ink tracking-heading">{title}</h3>
            <p className="mt-1.5 text-[13px] text-ink-muted leading-relaxed">{message}</p>
          </div>
        </div>

        <div className="px-5 pb-5 flex justify-end gap-2">
          <button ref={cancelRef} onClick={onCancel} className="btn-ghost">
            {cancelLabel}
          </button>
          <button onClick={onConfirm} className={variant === 'danger' ? 'btn-danger' : 'btn-primary'}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
