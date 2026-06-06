import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Notification } from "@/lib/types";

const { push, updateTask, bulkRejectTasks } = vi.hoisted(() => ({
  push: vi.fn(),
  updateTask: vi.fn(),
  bulkRejectTasks: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api", () => ({
  updateTask,
  bulkRejectTasks,
}));

import { NotificationItem } from "../NotificationItem";

function buildZombieNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "notif-zombie-1",
    user_id: "usr-1",
    event_id: null,
    type: "task_zombie_report",
    title: "5 task approved da >21gg in acme",
    body: JSON.stringify({
      project: "acme",
      count: 5,
      threshold_days: 21,
      task_ids: ["uuid-1", "uuid-2", "uuid-3", "uuid-4", "uuid-5"],
      samples: [
        { id: "uuid-1", title: "Old task", age_days: 35 },
      ],
    }),
    target_type: "task",
    // target_id is NULL server-side for zombie reports; typed as string,
    // cast because the render path for this type must never call updateTask.
    target_id: "",
    project: "acme",
    read_at: null,
    acted_at: null,
    created_at: "2026-04-21T10:00:00Z",
    ...overrides,
  };
}

describe("NotificationItem — task_zombie_report", () => {
  beforeEach(() => {
    push.mockReset();
    updateTask.mockReset();
    bulkRejectTasks.mockReset();
    vi.restoreAllMocks();
  });

  it("renders reject button with count when body is valid", () => {
    render(
      <NotificationItem
        notification={buildZombieNotification()}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    expect(
      screen.getByRole("button", { name: /rifiuta 5 zombie/i })
    ).toBeInTheDocument();
    // View button must NOT appear for zombie reports (no single task target)
    expect(screen.queryByRole("button", { name: /view/i })).not.toBeInTheDocument();
  });

  it("does not render button when body is malformed JSON", () => {
    render(
      <NotificationItem
        notification={buildZombieNotification({ body: "not-json" })}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    expect(screen.queryByRole("button", { name: /rifiuta/i })).not.toBeInTheDocument();
  });

  it("does not render button when task_ids is empty", () => {
    render(
      <NotificationItem
        notification={buildZombieNotification({
          body: JSON.stringify({ project: "x", count: 0, task_ids: [] }),
        })}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    expect(screen.queryByRole("button", { name: /rifiuta/i })).not.toBeInTheDocument();
  });

  it("calls bulkRejectTasks with task_ids and aging_zombie reason on confirm", async () => {
    const user = userEvent.setup();
    const onMarkActed = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    bulkRejectTasks.mockResolvedValue({
      rejected: ["uuid-1", "uuid-2", "uuid-3", "uuid-4", "uuid-5"],
      failed: [],
      total: 5,
    });

    render(
      <NotificationItem
        notification={buildZombieNotification()}
        onMarkRead={vi.fn()}
        onMarkActed={onMarkActed}
        onClose={vi.fn()}
      />
    );

    await user.click(screen.getByRole("button", { name: /rifiuta 5 zombie/i }));

    expect(bulkRejectTasks).toHaveBeenCalledWith(
      ["uuid-1", "uuid-2", "uuid-3", "uuid-4", "uuid-5"],
      "aging_zombie"
    );
    expect(onMarkActed).toHaveBeenCalledWith("notif-zombie-1", "bulk_rejected", undefined);
  });

  it("does not call API when user cancels the confirm dialog", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(false);

    render(
      <NotificationItem
        notification={buildZombieNotification()}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    await user.click(screen.getByRole("button", { name: /rifiuta 5 zombie/i }));

    expect(bulkRejectTasks).not.toHaveBeenCalled();
  });

  it("surfaces partial failure count when some tasks fail", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    bulkRejectTasks.mockResolvedValue({
      rejected: ["uuid-1", "uuid-2"],
      failed: [
        { task_id: "uuid-3", error: "not_found" },
        { task_id: "uuid-4", error: "invalid_transition from pending" },
        { task_id: "uuid-5", error: "not_found" },
      ],
      total: 5,
    });

    render(
      <NotificationItem
        notification={buildZombieNotification()}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    await user.click(screen.getByRole("button", { name: /rifiuta 5 zombie/i }));

    expect(await screen.findByText(/2 rejected, 3 failed/i)).toBeInTheDocument();
  });

  it("shows error text when bulkRejectTasks rejects", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    bulkRejectTasks.mockRejectedValue(new Error("HTTP 500: boom"));

    render(
      <NotificationItem
        notification={buildZombieNotification()}
        onMarkRead={vi.fn()}
        onMarkActed={vi.fn()}
        onClose={vi.fn()}
      />
    );

    await user.click(screen.getByRole("button", { name: /rifiuta 5 zombie/i }));

    expect(await screen.findByText(/HTTP 500: boom/i)).toBeInTheDocument();
  });
});
