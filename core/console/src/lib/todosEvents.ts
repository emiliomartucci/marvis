export const TODOS_CHANGED_EVENT = "marvisx:todos_changed";

export function notifyTodosChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(TODOS_CHANGED_EVENT));
}
