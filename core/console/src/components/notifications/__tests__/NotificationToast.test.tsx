import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Notification } from "@/lib/types";

const { push, updateTask } = vi.hoisted(() => ({
  push: vi.fn(),
  updateTask: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("@/lib/api", () => ({
  updateTask,
}));

import { NotificationToastStack } from "../NotificationToast";

function buildNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "notif-1",
    user_id: "usr-1",
    event_id: "evt-1",
    type: "task_pending",
    title: "New task pending",
    body: "Review needed",
    target_type: "task",
    target_id: "task-123",
    project: "marvisx",
    read_at: null,
    acted_at: null,
    created_at: "2026-04-09T10:00:00Z",
    ...overrides,
  };
}

describe("NotificationToastStack", () => {
  beforeEach(() => {
    push.mockReset();
    updateTask.mockReset();
  });

  it("marks the notification as acted after approve succeeds", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const onMarkRead = vi.fn();
    const onMarkActed = vi.fn();
    updateTask.mockResolvedValue({});

    render(
      <NotificationToastStack
        toasts={[buildNotification()]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(updateTask).toHaveBeenCalledWith("task-123", { status: "approved" });
    expect(onMarkActed).toHaveBeenCalledWith("notif-1", "approved", "task-123");
    expect(onMarkRead).not.toHaveBeenCalled();
  });

  it("marks the notification as read and deep-links to the task on view", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const onMarkRead = vi.fn();
    const onMarkActed = vi.fn();

    render(
      <NotificationToastStack
        toasts={[buildNotification()]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    await user.click(screen.getByRole("button", { name: "View" }));

    expect(onMarkRead).toHaveBeenCalledWith("notif-1");
    expect(onMarkActed).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/triage/?task=task-123");
  });

  it("shows inline error and does not dismiss when the approve call fails", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const onMarkRead = vi.fn();
    const onMarkActed = vi.fn();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    updateTask.mockRejectedValue(new Error("HTTP 500: boom"));

    render(
      <NotificationToastStack
        toasts={[buildNotification()]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(updateTask).toHaveBeenCalledWith("task-123", { status: "approved" });
    expect(onMarkActed).not.toHaveBeenCalled();
    expect(onDismiss).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Error: HTTP 500: boom");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    consoleError.mockRestore();
  });

  it("refuses to call updateTask and surfaces an error when target_id is missing", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const onMarkRead = vi.fn();
    const onMarkActed = vi.fn();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <NotificationToastStack
        toasts={[buildNotification({ target_id: "" })]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(updateTask).not.toHaveBeenCalled();
    expect(onMarkActed).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Error: Missing task id");
    consoleError.mockRestore();
  });

  it("shows the auto-approved badge only for auto-approved notifications", () => {
    const onDismiss = vi.fn();
    const onMarkRead = vi.fn();
    const onMarkActed = vi.fn();

    const { rerender } = render(
      <NotificationToastStack
        toasts={[buildNotification({ acted_at: "2026-04-09T10:05:00Z" })]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    expect(screen.queryByText("Auto-approved")).not.toBeInTheDocument();

    rerender(
      <NotificationToastStack
        toasts={[buildNotification({ type: "task_auto_approved", acted_at: "2026-04-09T10:05:00Z" })]}
        onDismiss={onDismiss}
        onMarkRead={onMarkRead}
        onMarkActed={onMarkActed}
      />
    );

    expect(screen.getByText("Auto-approved")).toBeInTheDocument();
  });
});
