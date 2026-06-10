"use client";

interface ActionViewIconProps {
  unreadCount: number;
  onClick: () => void;
  isOpen: boolean;
}

export function ActionViewIcon({ unreadCount, onClick, isOpen }: ActionViewIconProps) {
  return (
    <div className="relative">
      <button
        onClick={onClick}
        className={`p-1.5 rounded transition-colors ${
          isOpen
            ? "text-pir-accent"
            : "text-pir-text-muted hover:text-pir-text-secondary"
        }`}
        aria-label={`Inbox action view${unreadCount > 0 ? ` (${unreadCount} unread)` : ""}`}
      >
        {/* Mail/inbox icon */}
        <svg
          xmlns="http://www.w3.org/2000/svg"
          className="w-4 h-4"
          viewBox="0 0 20 20"
          fill="currentColor"
        >
          <path d="M2.003 5.884L10 9.882l7.997-3.998A2 2 0 0016 4H4a2 2 0 00-1.997 1.884z" />
          <path d="M18 8.118l-8 4-8-4V14a2 2 0 002 2h12a2 2 0 002-2V8.118z" />
        </svg>

        {/* Badge */}
        {unreadCount > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 flex items-center justify-center rounded-full bg-pir-accent text-white text-[9px] font-bold leading-none"
          >
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>
    </div>
  );
}
