"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  getInboxDigestItems,
  getInboxDigestStats,
  getInboxItem,
  patchInboxItemStatus,
  generateTldr,
  generateDeepResearch,
  getSourceScores,
} from "@/lib/api";
import type { SourceScore } from "@/lib/api";
import type { InboxItemSummary, InboxItemDetail, InboxIgnoreReason, InboxStatus } from "@/lib/types";

const POLL_INTERVAL = 30_000;

// --- Types ---

interface Decision {
  status: InboxStatus;
  ignoreReason?: InboxIgnoreReason;
}

interface PatchJob {
  itemId: string;
  decision: Decision;
  inPlace?: boolean;
}

interface CardState {
  items: InboxItemSummary[];
  currentIndex: number;
  decisions: Record<string, Decision>;
  undoStack: { itemId: string; prev: Decision | null }[];
  pending: PatchJob[];
  inflight: boolean;
  error: string | null;
  toastMessage: string | null;
}

type CardAction =
  | { type: "LOAD_ITEMS"; items: InboxItemSummary[] }
  | { type: "DECIDE"; itemId: string; decision: Decision }
  | { type: "DECIDE_IN_PLACE"; itemId: string; newStatus: InboxStatus }
  | { type: "DECIDE_IN_PLACE_FAIL"; itemId: string; error: string }
  | { type: "UNDO" }
  | { type: "NAVIGATE"; index: number }
  | { type: "PATCH_START" }
  | { type: "PATCH_DONE"; itemId: string }
  | { type: "PATCH_FAIL"; itemId: string; error: string }
  | { type: "CLEAR_ERROR" }
  | { type: "CLEAR_TOAST" };

function statusLabel(status: InboxStatus): string {
  switch (status) {
    case "saved":
      return "Salvato";
    case "newsletter":
      return "Aggiunto alla newsletter";
    case "idea":
      return "Marcato come idea";
    case "preferred":
      return "Marcato come preferito";
    case "read":
      return "Segnato come letto";
    case "unread":
      return "Riportato in da leggere";
    case "ignored":
      return "Ignorato";
    case "auto_ignored":
      return "Auto-ignorato";
    default:
      return `Marcato come ${status}`;
  }
}

function cardReducer(state: CardState, action: CardAction): CardState {
  switch (action.type) {
    case "LOAD_ITEMS":
      return {
        ...state,
        items: action.items,
        currentIndex: 0,
        decisions: {},
        undoStack: [],
        pending: [],
        inflight: false,
        error: null,
        toastMessage: null,
      };

    case "DECIDE": {
      const prevDecision = state.decisions[action.itemId] ?? null;
      const newDecisions = { ...state.decisions, [action.itemId]: action.decision };
      const nextIndex = Math.min(state.currentIndex + 1, state.items.length);
      const job: PatchJob = { itemId: action.itemId, decision: action.decision };
      return {
        ...state,
        decisions: newDecisions,
        currentIndex: nextIndex,
        undoStack: [...state.undoStack, { itemId: action.itemId, prev: prevDecision }],
        pending: [...state.pending, job],
        error: null,
      };
    }

    case "DECIDE_IN_PLACE": {
      // Optimistic in-place status change: stay on the same card,
      // update the local item and queue the PATCH via the existing pipeline.
      const items = state.items.map((it) =>
        it.id === action.itemId ? { ...it, status: action.newStatus } : it
      );
      const prevDecision = state.decisions[action.itemId] ?? null;
      const newDecisions = {
        ...state.decisions,
        [action.itemId]: { status: action.newStatus },
      };
      const job: PatchJob = {
        itemId: action.itemId,
        decision: { status: action.newStatus },
        inPlace: true,
      };
      return {
        ...state,
        items,
        decisions: newDecisions,
        undoStack: [
          ...state.undoStack,
          { itemId: action.itemId, prev: prevDecision },
        ],
        pending: [...state.pending, job],
        toastMessage: statusLabel(action.newStatus),
        error: null,
      };
    }

    case "DECIDE_IN_PLACE_FAIL":
      return {
        ...state,
        toastMessage: `Errore: ${action.error}`,
      };

    case "UNDO": {
      if (state.undoStack.length === 0) return state;
      const lastEntry = state.undoStack[state.undoStack.length - 1];
      const newStack = state.undoStack.slice(0, -1);
      const newDecisions = { ...state.decisions };
      if (lastEntry.prev) {
        newDecisions[lastEntry.itemId] = lastEntry.prev;
      } else {
        delete newDecisions[lastEntry.itemId];
      }
      // Navigate back to the undone item
      const undoneIndex = state.items.findIndex((item) => item.id === lastEntry.itemId);
      // Queue a PATCH to reset status to unread
      const undoJob: PatchJob = { itemId: lastEntry.itemId, decision: { status: "unread" } };
      return {
        ...state,
        decisions: newDecisions,
        undoStack: newStack,
        currentIndex: undoneIndex >= 0 ? undoneIndex : Math.max(0, state.currentIndex - 1),
        pending: [...state.pending, undoJob],
        error: null,
      };
    }

    case "NAVIGATE":
      return { ...state, currentIndex: action.index };

    case "PATCH_START":
      return { ...state, inflight: true };

    case "PATCH_DONE": {
      const newPending = state.pending.filter((j) => j.itemId !== action.itemId);
      return { ...state, pending: newPending, inflight: newPending.length > 0, error: null };
    }

    case "PATCH_FAIL":
      return {
        ...state,
        inflight: false,
        error: action.error,
      };

    case "CLEAR_ERROR":
      return { ...state, error: null };

    case "CLEAR_TOAST":
      return { ...state, toastMessage: null };

    default:
      return state;
  }
}

const initialCardState: CardState = {
  items: [],
  currentIndex: 0,
  decisions: {},
  undoStack: [],
  pending: [],
  inflight: false,
  error: null,
  toastMessage: null,
};

// --- Hook ---

export function useActionView() {
  const [unreadCount, setUnreadCount] = useState(0);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [state, dispatch] = useReducer(cardReducer, initialCardState);
  const [currentDetail, setCurrentDetail] = useState<InboxItemDetail | null>(null);
  const [tldr, setTldr] = useState<string | null>(null);
  const [tldrLoading, setTldrLoading] = useState(false);
  const [deepResearch, setDeepResearch] = useState<string | null>(null);
  const [deepResearchLoading, setDeepResearchLoading] = useState(false);
  const [sourceScores, setSourceScores] = useState<SourceScore[]>([]);
  const controllerRef = useRef<AbortController | null>(null);
  const detailControllerRef = useRef<AbortController | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const processingRef = useRef(false);

  // --- Fetch full item detail when currentIndex changes ---
  useEffect(() => {
    const item = state.items[state.currentIndex];
    if (!item) {
      setCurrentDetail(null);
      return;
    }

    // Clear previous detail, TL;DR, and deep research so stale content doesn't linger
    setCurrentDetail(null);
    setTldr(null);
    setTldrLoading(false);
    setDeepResearch(null);
    setDeepResearchLoading(false);

    // Abort any in-flight detail fetch
    if (detailControllerRef.current) {
      detailControllerRef.current.abort();
    }
    const controller = new AbortController();
    detailControllerRef.current = controller;

    void (async () => {
      try {
        const detail = await getInboxItem(item.id, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setCurrentDetail(detail);
          // Restore cached TL;DR and deep research from the detail response
          if (detail.tldr) setTldr(detail.tldr);
          if (detail.deep_research) setDeepResearch(detail.deep_research);
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Silently fail — the card still shows the summary
      }
    })();

    return () => {
      controller.abort();
    };
  }, [state.currentIndex, state.items]);

  // --- Unread count polling ---
  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;

    const fetchCount = async () => {
      try {
        const data = await getInboxDigestStats({ signal: controller.signal });
        setUnreadCount(data.visible);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
      }
    };

    const startPolling = () => {
      void fetchCount();
      if (intervalRef.current) return;
      intervalRef.current = setInterval(fetchCount, POLL_INTERVAL);
    };

    const stopPolling = () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };

    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        startPolling();
      } else {
        stopPolling();
      }
    };

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      controller.abort();
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  // --- Serial action queue ---
  useEffect(() => {
    if (processingRef.current || state.pending.length === 0) return;

    const processQueue = async () => {
      processingRef.current = true;
      const jobs = [...state.pending];

      for (const job of jobs) {
        dispatch({ type: "PATCH_START" });
        try {
          await patchInboxItemStatus(job.itemId, {
            status: job.decision.status,
            ignore_reason: job.decision.ignoreReason,
          });
          dispatch({ type: "PATCH_DONE", itemId: job.itemId });
          // Update unread count optimistically
          if (job.decision.status !== "unread") {
            setUnreadCount((prev) => Math.max(0, prev - 1));
          } else {
            setUnreadCount((prev) => prev + 1);
          }
        } catch (e) {
          const errorMsg =
            e instanceof Error ? e.message : "Failed to update status";
          // Log error (no silent failure)
          console.error(
            `[useActionView] PATCH failed for item ${job.itemId}:`,
            errorMsg
          );
          if (job.inPlace) {
            // In-place jobs surface errors via the toast instead of state.error
            dispatch({
              type: "DECIDE_IN_PLACE_FAIL",
              itemId: job.itemId,
              error: errorMsg,
            });
            dispatch({ type: "PATCH_DONE", itemId: job.itemId });
            continue;
          }
          dispatch({
            type: "PATCH_FAIL",
            itemId: job.itemId,
            error: errorMsg,
          });
          break;
        }
      }
      processingRef.current = false;
    };

    void processQueue();
  }, [state.pending]);

  // --- Open modal: resume existing state or fetch fresh items ---
  const openModal = useCallback(async () => {
    setIsOpen(true);
    const controller = new AbortController();

    // If we already have items and some are still unprocessed, resume
    const hasUnprocessed = state.items.length > 0 && state.currentIndex < state.items.length;
    if (hasUnprocessed) {
      return;
    }

    setLoading(true);
    // Load source scores in parallel
    void getSourceScores().then(setSourceScores).catch(() => setSourceScores([]));
    try {
      const items = await getInboxDigestItems({
        limit: 200,
        signal: controller.signal,
      });
      dispatch({ type: "LOAD_ITEMS", items });
    } catch {
      dispatch({ type: "LOAD_ITEMS", items: [] });
    } finally {
      setLoading(false);
    }
  }, [state.items.length, state.currentIndex]);

  const closeModal = useCallback(() => {
    setIsOpen(false);
  }, []);

  // --- TL;DR generation ---
  const requestTldr = useCallback(async () => {
    const item = state.items[state.currentIndex];
    if (!item || tldrLoading) return;
    setTldrLoading(true);
    try {
      const result = await generateTldr(item.id);
      setTldr(result.tldr);
    } catch (e) {
      dispatch({
        type: "PATCH_FAIL",
        itemId: item.id,
        error: e instanceof Error ? e.message : "TL;DR generation failed",
      });
    } finally {
      setTldrLoading(false);
    }
  }, [state.items, state.currentIndex, tldrLoading]);

  // --- Deep Research generation ---
  const requestDeepResearch = useCallback(async () => {
    const item = state.items[state.currentIndex];
    if (!item || deepResearchLoading) return;
    setDeepResearchLoading(true);
    try {
      const result = await generateDeepResearch(item.id);
      setDeepResearch(result.deep_research);
    } catch (e) {
      dispatch({
        type: "PATCH_FAIL",
        itemId: item.id,
        error: e instanceof Error ? e.message : "Deep research generation failed",
      });
    } finally {
      setDeepResearchLoading(false);
    }
  }, [state.items, state.currentIndex, deepResearchLoading]);

  // --- Decision functions ---
  const decide = useCallback(
    (status: InboxStatus, ignoreReason?: InboxIgnoreReason) => {
      const currentItem = state.items[state.currentIndex];
      if (!currentItem) return;
      dispatch({
        type: "DECIDE",
        itemId: currentItem.id,
        decision: { status, ignoreReason },
      });
    },
    [state.items, state.currentIndex]
  );

  const undo = useCallback(() => {
    dispatch({ type: "UNDO" });
  }, []);

  const clearError = useCallback(() => {
    dispatch({ type: "CLEAR_ERROR" });
  }, []);

  // Save without advancing — stays on the same card, queues the PATCH via the
  // existing serial pipeline and shows a toast for feedback.
  const saveInPlace = useCallback(() => {
    const currentItem = state.items[state.currentIndex];
    if (!currentItem) return;
    dispatch({
      type: "DECIDE_IN_PLACE",
      itemId: currentItem.id,
      newStatus: "saved",
    });
  }, [state.items, state.currentIndex]);

  const clearToast = useCallback(() => {
    dispatch({ type: "CLEAR_TOAST" });
  }, []);

  // Auto-dismiss toast after 2 seconds
  useEffect(() => {
    if (!state.toastMessage) return;
    const id = setTimeout(() => dispatch({ type: "CLEAR_TOAST" }), 2000);
    return () => clearTimeout(id);
  }, [state.toastMessage]);

  const currentItem = state.items[state.currentIndex] ?? null;
  const totalItems = state.items.length;
  const isExhausted = state.currentIndex >= totalItems;

  return {
    unreadCount,
    isOpen,
    loading,
    currentItem,
    currentDetail,
    currentIndex: state.currentIndex,
    totalItems,
    isExhausted,
    error: state.error,
    toastMessage: state.toastMessage,
    tldr,
    tldrLoading,
    deepResearch,
    deepResearchLoading,
    sourceScores,
    openModal,
    closeModal,
    decide,
    undo,
    clearError,
    clearToast,
    requestTldr,
    requestDeepResearch,
    saveInPlace,
  };
}
