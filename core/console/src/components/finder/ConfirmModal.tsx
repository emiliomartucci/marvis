"use client";

interface ConfirmModalProps {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmModal({
  title,
  message,
  confirmLabel = "Confirm",
  danger = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-pir-surface-0 border border-pir rounded-lg w-[360px]">
        <div className="px-4 py-3 border-b border-pir">
          <h3 className="text-label text-pir-text-primary">{title}</h3>
        </div>
        <div className="p-4">
          <p className="text-caption text-pir-text-secondary">{message}</p>
        </div>
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-pir">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={`px-3 py-1.5 text-caption text-white rounded transition-colors ${
              danger
                ? "bg-red-500 hover:bg-red-400"
                : "bg-pir-accent hover:bg-pir-accent/90"
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
