import { describe, expect, it } from "vitest";
import type { TodoResponseLocal } from "@/lib/api";
import {
  groupTodosByHorizon,
  matchesTodoFilter,
  nextHorizonDate,
  todoActionDefinitions,
  todoRowActionDefinitions,
  todoRowControl,
} from "../todoModel";

function todo(overrides: Partial<TodoResponseLocal>): TodoResponseLocal {
  return {
    id: "todo-1",
    type: "promemoria",
    family: "captured",
    status: "aperto",
    text: "Call Marco",
    payload: null,
    fu: "2026-06-12",
    project: null,
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

describe("todoModel", () => {
  it("groups FU dates by horizon including the week boundary", () => {
    const base = new Date("2026-06-12T12:00:00");
    const groups = groupTodosByHorizon(
      [
        todo({ id: "late", fu: "2026-06-11" }),
        todo({ id: "today", fu: "2026-06-12" }),
        todo({ id: "tomorrow", fu: "2026-06-13" }),
        todo({ id: "week", fu: "2026-06-19" }),
        todo({ id: "later", fu: "2026-06-20" }),
      ],
      base
    );

    expect(groups.map((group) => [group.horizon, group.items.map((item) => item.id)])).toEqual([
      ["overdue", ["late"]],
      ["today", ["today"]],
      ["tomorrow", ["tomorrow"]],
      ["week", ["week"]],
      ["later", ["later"]],
    ]);
  });

  it("computes the next postpone date from the current horizon", () => {
    const base = new Date("2026-06-12T12:00:00");
    expect(nextHorizonDate("2026-06-12", base)).toBe("2026-06-13");
    expect(nextHorizonDate("2026-06-13", base)).toBe("2026-06-19");
    expect(nextHorizonDate("2026-06-19", base)).toBe("2026-06-26");
  });

  it("filters decision and brain-origin todos", () => {
    expect(matchesTodoFilter(todo({ type: "decidi" }), "decisions")).toBe(true);
    expect(matchesTodoFilter(todo({ source: "brain" }), "brain")).toBe(true);
    expect(matchesTodoFilter(todo({ source: "user" }), "brain")).toBe(false);
  });

  it("hides Delegate when the doer is human", () => {
    const agentActions = todoActionDefinitions(todo({ type: "azione", doer: "agent" })).map((action) => action.id);
    const humanActions = todoActionDefinitions(todo({ type: "azione", doer: "human" })).map((action) => action.id);

    expect(agentActions).toContain("delegate");
    expect(humanActions).not.toContain("delegate");
  });

  it("gives the row checkbox only to types whose state machine allows fatto", () => {
    expect(todoRowControl(todo({ type: "promemoria" }))).toBe("checkbox");
    expect(todoRowControl(todo({ type: "azione" }))).toBe("checkbox");
    expect(todoRowControl(todo({ type: "idea" }))).toBe("gate");
    expect(todoRowControl(todo({ type: "decidi" }))).toBe("gate");
    expect(todoRowControl(todo({ type: "approva" }))).toBe("gate");
    expect(todoRowControl(todo({ type: "rivedi" }))).toBe("gate");
    expect(todoRowControl(todo({ type: "azione", virtual: true }))).toBe("gate");
  });

  it("keeps complete in the checkbox and discard on-row only for ideas", () => {
    const promemoria = todoRowActionDefinitions(todo({ type: "promemoria" })).map((action) => action.id);
    const azione = todoRowActionDefinitions(todo({ type: "azione", doer: "agent" })).map((action) => action.id);
    const idea = todoRowActionDefinitions(todo({ type: "idea" })).map((action) => action.id);

    expect(promemoria).toEqual(["postpone"]);
    expect(azione).toEqual(["delegate", "postpone"]);
    expect(idea).toEqual(["promote", "postpone", "discard"]);
  });
});
