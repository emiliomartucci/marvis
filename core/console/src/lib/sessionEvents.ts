export const SESSION_COUNT_CHANGED_EVENT = "marvisx:sessions_count_changed";

export interface SessionsCountChangedDetail {
  count: number;
}

export function dispatchSessionsCountChanged(count: number) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<SessionsCountChangedDetail>(SESSION_COUNT_CHANGED_EVENT, {
      detail: { count },
    }),
  );
}

export function readSessionsCountChangedDetail(event: Event) {
  return (event as CustomEvent<SessionsCountChangedDetail>).detail;
}
