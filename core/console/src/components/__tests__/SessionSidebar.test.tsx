import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// Mock API module
vi.mock("@/lib/api", () => ({
  listSessions: vi.fn().mockResolvedValue([]),
  deleteSession: vi.fn(),
  completeSession: vi.fn(),
  updateSession: vi.fn(),
  reorderSessions: vi.fn(),
  resurrectSession: vi.fn(),
  hibernateSession: vi.fn(),
  resumeSession: vi.fn(),
  restartSession: vi.fn(),
  getCostsSummary: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/auth", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({
    status: "authenticated",
    permissions: { canWrite: true, canAdmin: false, canView: true },
  }),
}));

vi.mock("@/components/PermissionGate", () => ({
  PermissionGate: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("../CreateSessionModal", () => ({
  default: () => <div data-testid="create-session-modal">Create Modal</div>,
}));

vi.mock("../ProjectSelectorModal", () => ({
  default: () => <div data-testid="project-selector-modal">Project Selector</div>,
}));

import { listSessions, getCostsSummary } from "@/lib/api";
import type { Session } from "@/lib/types";
import SessionSidebar, { nextSessionRefreshDelayMs } from "../SessionSidebar";

const mockSession: Session = {
  name: "session-1",
  display_name: "My Session",
  pinned: false,
  sort_order: 0,
  group_name: null,
  project_slug: null,
  session_uuid: "uuid-1",
  status: "claude",
  created_at: "2026-03-01T00:00:00Z",
  last_active: "2026-03-01T01:00:00Z",
  attached: false,
  hibernated: false,
  conversation_id: null,
  model: "claude-sonnet-4-20250514",
  last_context_pct: 45,
  last_cost_usd: 0.32,
  last_message_count: 10,
  auto_hibernate_minutes: 30,
  activity_state: "idle",
  cpu_pct: 3.2,
  ram_mb: 256,
  working_seconds: 600,
  created_epoch: Math.floor(Date.now() / 1000) - 3600,
  completed_at: null,
  agent_managed: false,
};

const mockPinnedSession: Session = {
  ...mockSession,
  name: "session-pinned",
  display_name: "Pinned",
  pinned: true,
};

const mockHibernatedSession: Session = {
  ...mockSession,
  name: "session-hibernated",
  display_name: "Sleeping",
  hibernated: true,
};

const mockGroupedSession: Session = {
  ...mockSession,
  name: "session-grouped",
  project_slug: "marvisx",
};

const defaultProps = {
  activeSession: null as string | null,
  openSessions: [] as string[],
  onSelectSession: vi.fn(),
  onSessionCreated: vi.fn(),
  onSessionDeleted: vi.fn(),
};

describe("SessionSidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listSessions).mockResolvedValue([]);
    vi.mocked(getCostsSummary).mockResolvedValue([]);
  });

  it("renders empty state when no sessions", async () => {
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("No sessions yet")).toBeInTheDocument();
    });
  });

  it("renders header with Sessions title and New button", () => {
    render(<SessionSidebar {...defaultProps} />);
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    expect(screen.getByText("+ New")).toBeInTheDocument();
  });

  it("renders session names when sessions exist", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession, mockPinnedSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("session-1")).toBeInTheDocument();
      expect(screen.getByText("session-pinned")).toBeInTheDocument();
    });
  });

  it("renders display_name below session name", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("My Session")).toBeInTheDocument();
    });
  });

  it("shows ZZZ indicator for hibernated sessions", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockHibernatedSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("ZZZ")).toBeInTheDocument();
    });
  });

  it("renders model shortname for sessions with model", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText(/snnt/)).toBeInTheDocument();
    });
  });

  it("renders context percentage bar when last_context_pct is set", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("45%")).toBeInTheDocument();
    });
  });

  it("renders cost when last_cost_usd is set", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("$0.32")).toBeInTheDocument();
    });
  });

  it("renders CPU and RAM metrics", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("3.2%")).toBeInTheDocument();
      expect(screen.getByText("256M")).toBeInTheDocument();
    });
  });

  it("renders grouped sessions under group header", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockGroupedSession, mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      // "marvisx" group label (uppercase is CSS)
      expect(screen.getByText("marvisx")).toBeInTheDocument();
      // "Ungrouped" for sessions without project_slug
      expect(screen.getByText("Ungrouped")).toBeInTheDocument();
    });
  });

  it("highlights the active session", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} activeSession="session-1" />);
    await waitFor(() => {
      const sessionEl = screen.getByText("session-1").closest(".border-l-2");
      expect(sessionEl?.className).toContain("bg-pir-surface-1");
    });
  });

  it("jitters session refresh delay above the cache TTL cadence", () => {
    expect(nextSessionRefreshDelayMs(() => 0)).toBe(15_000);
    expect(nextSessionRefreshDelayMs(() => 0.5)).toBe(16_500);
    expect(nextSessionRefreshDelayMs(() => 0.999)).toBe(17_997);
  });

  it("patches session name in-place on session_renamed WS event (Plan 2026-05-21)", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("session-1")).toBeInTheDocument();
    });

    // Simulate WS broadcast session_renamed event with delta payload
    window.dispatchEvent(
      new CustomEvent("marvisx:sessions_changed", {
        detail: {
          type: "sessions_changed",
          event: "renamed",
          old_name: "session-1",
          new_name: "session-1-renamed",
          session_info: {
            name: "session-1-renamed",
            prev_name: "session-1",
            provider: "claude",
            model: "claude-sonnet-4-20250514",
            project_slug: null,
            display_name: "My Session",
            updated_at: "2026-05-21T12:00:00+00:00",
          },
        },
      })
    );

    // Sidebar must show new name WITHOUT calling listSessions (no refetch)
    await waitFor(() => {
      expect(screen.getByText("session-1-renamed")).toBeInTheDocument();
    });
    expect(screen.queryByText("session-1")).not.toBeInTheDocument();

    // Verify listSessions was NOT called as a result of the rename (initial
    // mount call is allowed; rename should not trigger a second one)
    expect(vi.mocked(listSessions).mock.calls.length).toBeLessThanOrEqual(1);
  });

  it("ignores session_renamed for unknown session (no crash)", async () => {
    vi.mocked(listSessions).mockResolvedValue([mockSession]);
    render(<SessionSidebar {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("session-1")).toBeInTheDocument();
    });

    window.dispatchEvent(
      new CustomEvent("marvisx:sessions_changed", {
        detail: {
          type: "sessions_changed",
          event: "renamed",
          old_name: "nonexistent",
          new_name: "whatever",
          session_info: {
            name: "whatever",
            prev_name: "nonexistent",
            updated_at: "2026-05-21T12:00:00+00:00",
          },
        },
      })
    );

    // session-1 remains untouched
    await waitFor(() => {
      expect(screen.getByText("session-1")).toBeInTheDocument();
    });
  });
});
