import { API_BASE_URL } from "@/lib/config";

export interface GuiFirstValueInput {
  surface: "brain_diario";
  route: string;
  cycle_key: string;
  run_id: string;
  entry_id: string;
  registri_count: number;
  scope_type?: string | null;
  scope_key?: string | null;
}

export interface GuiFirstValueResponse {
  event_name: "gui_first_value";
  emitted: boolean;
  event_id: string;
  seen_count: number;
  first_seen_at: string;
}

export async function emitGuiFirstValue(
  input: GuiFirstValueInput,
  opts?: { signal?: AbortSignal },
): Promise<GuiFirstValueResponse> {
  const res = await fetch(`${API_BASE_URL}/api/v1/gui/events/first-value`, {
    method: "POST",
    credentials: "include",
    signal: opts?.signal,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(input),
  });

  if (!res.ok) {
    throw new Error(`gui_first_value failed with HTTP ${res.status}`);
  }

  return (await res.json()) as GuiFirstValueResponse;
}
