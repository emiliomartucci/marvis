import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { applyVirtualTodoActionLocal, type TodoResponseLocal } from "@/lib/api";
import { it as itDict } from "@/lib/i18n/it";
import { TodoActionBar, TodosSurface } from "../TodosSurface";

function response(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function todo(overrides: Partial<TodoResponseLocal> = {}): TodoResponseLocal {
  return {
    id: "todo-1",
    type: "azione",
    family: "captured",
    status: "aperto",
    text: "Rivedere il piano",
    payload: null,
    fu: "2026-06-12",
    project: "marvisx",
    source: "user",
    source_ref: null,
    doer: "agent",
    linked_task_id: null,
    created_at: "2026-06-12T08:00:00Z",
    updated_at: "2026-06-12T08:00:00Z",
    resolved_at: null,
    virtual: false,
    origin: null,
    ...overrides,
  };
}

describe("TodosSurface", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.setItem("marvis:locale", "it");
    fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/todos") && init?.method === "POST") {
        return Promise.resolve(response(todo({ text: "Mandare update" }), 201));
      }
      return Promise.resolve(response([]));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("renders the per-type action bar and hides Delegate for human doer", () => {
    const onAction = vi.fn();
    const { rerender } = render(
      <TodoActionBar todo={todo({ type: "azione", doer: "agent" })} onAction={onAction} t={itDict.todos} />,
    );

    expect(screen.getByRole("button", { name: "Delega" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Completa" })).toBeInTheDocument();

    rerender(<TodoActionBar todo={todo({ type: "azione", doer: "human" })} onAction={onAction} t={itDict.todos} />);

    expect(screen.queryByRole("button", { name: "Delega" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Completa" })).toBeInTheDocument();
  });

  it("routes virtual approva actions to their owning endpoints", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(response({ ok: true })));

    await applyVirtualTodoActionLocal(
      todo({
        id: "virtual:task_review:task-123",
        type: "approva",
        virtual: true,
        origin: { kind: "task_review", id: "task-123" },
      }),
      "approve"
    );
    await applyVirtualTodoActionLocal(
      todo({
        id: "virtual:finding:finding-1",
        type: "approva",
        virtual: true,
        origin: { kind: "finding", id: "finding-1" },
      }),
      "approve"
    );
    await applyVirtualTodoActionLocal(
      todo({
        id: "virtual:memory_op:mem-1",
        type: "approva",
        virtual: true,
        origin: { kind: "memory_op", id: "mem-1" },
      }),
      "reject",
      { feedback: "No" }
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/pull_requests/task-123/merge",
      expect.objectContaining({ method: "POST" })
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/brain/findings/finding-1",
      expect.objectContaining({ method: "PATCH" })
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/brain/memory-operations/mem-1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ approval_state: "rejected", reason: "No" }),
      })
    );
  });

  it("posts capture text and shows the classification refresh caption", async () => {
    const user = userEvent.setup();
    render(<TodosSurface />);

    await user.type(screen.getByLabelText("Scrivi un todo, una decisione o un promemoria..."), "Mandare update");
    await user.click(screen.getByRole("button", { name: "Aggiungi" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/todos",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ text: "Mandare update" }),
        })
      );
    });
    expect(await screen.findByText(/il refresh mostra tipo e progetto/i)).toBeInTheDocument();
  });

  it("renders virtual item action feedback for operator-session failures", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/v1/todos")) {
        return Promise.resolve(
          response([
            todo({
              id: "virtual:task_review:task-123",
              type: "approva",
              family: "system",
              text: "Approva task",
              virtual: true,
              origin: { kind: "task_review", id: "task-123" },
              payload: { branch: "feat/x", pr_status: "open" },
            }),
          ])
        );
      }
      return Promise.resolve(response({ detail: "Forbidden" }, 403));
    });

    render(<TodosSurface />);
    fireEvent.click(await screen.findByRole("button", { name: "Approva" }));

    expect(await screen.findByText("richiede sessione operatore")).toBeInTheDocument();
  });
});
