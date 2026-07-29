import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Drawer } from "../Drawer";

describe("Drawer", () => {
  it("renders header, body, and actions when open", () => {
    render(
      <Drawer
        open
        titleId="drawer-title"
        onClose={vi.fn()}
        header={<h2 id="drawer-title">Task detail</h2>}
        actions={<button type="button">Save</button>}
      >
        <p>Drawer body</p>
      </Drawer>
    );

    expect(screen.getByRole("dialog", { name: "Task detail" })).toBeInTheDocument();
    expect(screen.getByText("Drawer body")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("closes on overlay click and Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(
      <Drawer open onClose={onClose} header={<h2>Task detail</h2>}>
        <button type="button">Inside</button>
      </Drawer>
    );

    await user.click(screen.getByTestId("drawer-overlay"));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("traps focus inside the panel", async () => {
    const user = userEvent.setup();

    render(
      <>
        <button type="button">Outside</button>
        <Drawer
          open
          onClose={vi.fn()}
          header={<button type="button">First</button>}
          actions={<button type="button">Last</button>}
        >
          <button type="button">Middle</button>
        </Drawer>
      </>
    );

    await screen.findByRole("button", { name: "First" });
    await user.tab();
    expect(screen.getByRole("button", { name: "Middle" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Last" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "First" })).toHaveFocus();
  });
});
