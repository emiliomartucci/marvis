const SGR_MOUSE_RE = /^\x1b\[<(\d+);\d+;\d+[Mm]$/;

export function isSgrWheelInput(data: string) {
  const match = data.match(SGR_MOUSE_RE);
  if (!match) return false;
  const buttonCode = Number(match[1]);
  return buttonCode >= 64 && buttonCode <= 65;
}

interface SgrMouseCoalescerOptions {
  dispatchMs: number;
  onDispatch: (data: string) => void;
}

export class SgrMouseCoalescer {
  private readonly dispatchMs: number;
  private readonly onDispatch: (data: string) => void;
  private pendingWheelInput: string | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(options: SgrMouseCoalescerOptions) {
    this.dispatchMs = options.dispatchMs;
    this.onDispatch = options.onDispatch;
  }

  push(data: string) {
    if (!isSgrWheelInput(data)) {
      this.onDispatch(data);
      return;
    }

    this.pendingWheelInput = data;
    if (this.timer) return;
    this.timer = setTimeout(() => this.flush(), this.dispatchMs);
  }

  flush() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const input = this.pendingWheelInput;
    this.pendingWheelInput = null;
    if (input) this.onDispatch(input);
  }

  dispose() {
    this.flush();
  }
}
