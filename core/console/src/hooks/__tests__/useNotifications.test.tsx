import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Notification } from "@/lib/types";

const {
  listNotifications,
  getUnreadNotificationCount,
  markNotificationRead,
  markNotificationActed,
  markAllNotificationsRead,
} = vi.hoisted(() => ({
  listNotifications: vi.fn(),
  getUnreadNotificationCount: vi.fn(),
  markNotificationRead: vi.fn(),
  markNotificationActed: vi.fn(),
  markAllNotificationsRead: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listNotifications,
  getUnreadNotificationCount,
  markNotificationRead,
  markNotificationActed,
  markAllNotificationsRead,
}));

import { useNotifications } from "../useNotifications";

function buildNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "notif-1",
    user_id: "usr-emilio",
    event_id: "evt-1",
    type: "task_completed",
    title: "Task completed",
    body: "fix HTML worker Airtable fetch bottleneck causing PDF generation hangs",
    target_type: "task",
    target_id: "task-123",
    project: "marvisx",
    read_at: null,
    acted_at: null,
    created_at: "2026-04-13T13:41:18Z",
    ...overrides,
  };
}

describe("useNotifications", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    listNotifications.mockReset();
    getUnreadNotificationCount.mockReset();
    markNotificationRead.mockReset();
    markNotificationActed.mockReset();
    markAllNotificationsRead.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not re-toast a dismissed notification on later polls", async () => {
    const notification = buildNotification();
    let pollCount = 0;

    listNotifications.mockImplementation(async () => {
      pollCount += 1;
      return pollCount === 1 ? [] : [notification];
    });
    getUnreadNotificationCount.mockResolvedValue({ count: 1 });

    const { result } = renderHook(() => useNotifications());

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.toasts).toEqual([]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(result.current.toasts).toHaveLength(1);

    act(() => {
      result.current.dismissToast(notification.id, notification.target_id);
    });
    expect(result.current.toasts).toEqual([]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(pollCount).toBeGreaterThanOrEqual(3);
    expect(result.current.toasts).toEqual([]);
  });
});
