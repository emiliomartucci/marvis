"use client";

import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { deleteOnboardingDemo } from "@/lib/api";
import { useT } from "@/lib/i18n";
import {
  advanceTour,
  initialTourState,
  shouldSkipMissingAnchor,
  tourSteps,
  type TourPart,
  type TourStep,
} from "@/lib/tour";

interface SpotlightTourProps {
  part: TourPart;
  onClose: () => void;
  onDemoRemoved: () => void;
}

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
  right: number;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return;
    setReduced(media.matches);
    const onChange = () => setReduced(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

function measureAnchor(step: TourStep | null): Rect | null {
  if (!step?.anchor) return null;
  const element = document.querySelector<HTMLElement>(`[data-tour="${step.anchor}"]`);
  if (!element) return null;
  const rect = element.getBoundingClientRect();
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    right: rect.right,
  };
}

export function SpotlightTour({ part, onClose, onDemoRemoved }: SpotlightTourProps) {
  const { t } = useT();
  const router = useRouter();
  const steps = useMemo(() => tourSteps(part), [part]);
  const [machine, setMachine] = useState(() => initialTourState(part));
  const [rect, setRect] = useState<Rect | null>(null);
  const [clearingDemo, setClearingDemo] = useState(false);
  const [clearError, setClearError] = useState(false);
  const reducedMotion = usePrefersReducedMotion();
  const step = machine.completed ? null : steps[machine.index] ?? null;
  const final = Boolean(step?.final);
  const stepText = step ? t.tour.steps[step.id] : null;

  useEffect(() => {
    setMachine(initialTourState(part));
    setRect(null);
  }, [part]);

  useLayoutEffect(() => {
    if (!step) return;
    if (step.final) {
      setRect(null);
      return;
    }

    if (step.route) router.push(step.route);

    let cancelled = false;
    let cleanupGate: (() => void) | null = null;
    let tries = 0;
    let retryTimer = 0;
    const mountDelay = window.setTimeout(() => {
      const measure = () => {
        if (cancelled) return;
        const nextRect = measureAnchor(step);
        if (nextRect) {
          setRect(nextRect);
          if (step.gate) {
            const element = document.querySelector<HTMLElement>(`[data-tour="${step.anchor}"]`);
            const onGate = () => {
              window.setTimeout(() => {
                setMachine((current) => advanceTour(current, "gate", steps));
              }, 520);
            };
            element?.addEventListener("click", onGate, { once: true });
            cleanupGate = () => element?.removeEventListener("click", onGate);
          }
          return;
        }
        if (tries < 10) {
          tries += 1;
          retryTimer = window.setTimeout(measure, 60);
          return;
        }
        if (shouldSkipMissingAnchor(step)) {
          setMachine((current) => advanceTour(current, "skip", steps));
        } else {
          setRect(null);
        }
      };
      measure();
    }, 520);

    return () => {
      cancelled = true;
      window.clearTimeout(mountDelay);
      window.clearTimeout(retryTimer);
      cleanupGate?.();
    };
  }, [router, step, steps]);

  useEffect(() => {
    if (!step || step.final) return;
    const onMove = () => setRect(measureAnchor(step));
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [step]);

  if (!step || !stepText) return null;

  const viewportWidth = typeof window === "undefined" ? 1024 : window.innerWidth;
  const viewportHeight = typeof window === "undefined" ? 768 : window.innerHeight;
  const cardWidth = 320;
  const pad = 6;
  const hole = rect
    ? {
        left: rect.left - pad,
        top: rect.top - pad,
        width: rect.width + pad * 2,
        height: rect.height + pad * 2,
      }
    : null;
  let cardLeft = (viewportWidth - cardWidth) / 2;
  if (rect) {
    cardLeft = rect.right + 16;
    if (cardLeft + cardWidth > viewportWidth - 12) {
      cardLeft = Math.max(12, rect.left - cardWidth - 16);
    }
  }
  const cardTop = rect
    ? Math.max(14, Math.min(rect.top - 4, viewportHeight - 250))
    : Math.max(80, viewportHeight / 2 - 140);

  function next() {
    setMachine((current) => advanceTour(current, "next", steps));
  }

  function back() {
    setMachine((current) => ({ ...current, index: Math.max(0, current.index - 1) }));
  }

  async function clearDemo() {
    setClearingDemo(true);
    setClearError(false);
    try {
      await deleteOnboardingDemo();
      onDemoRemoved();
      onClose();
    } catch {
      setClearError(true);
    } finally {
      setClearingDemo(false);
    }
  }

  return (
    <div
      aria-label={t.tour.label}
      className="fixed inset-0 z-[120]"
      style={{ pointerEvents: "none" }}
    >
      {hole ? (
        <div
          aria-hidden
          className="fixed rounded border-2 border-pir-accent"
          style={{
            left: hole.left,
            top: hole.top,
            width: hole.width,
            height: hole.height,
            boxShadow: "0 0 0 9999px rgba(0,0,0,0.62)",
            transition: reducedMotion ? "none" : "all 220ms cubic-bezier(0.2,0,0,1)",
          }}
        />
      ) : (
        <div aria-hidden className="fixed inset-0 bg-black/60" />
      )}

      <section
        className="fixed rounded border border-pir-accent bg-pir-surface-0 p-4 text-pir-text-primary shadow-xl"
        style={{
          left: cardLeft,
          top: cardTop,
          width: cardWidth,
          pointerEvents: "auto",
          transition: reducedMotion ? "none" : "opacity 160ms ease, transform 160ms ease",
        }}
      >
        <div className="flex items-center justify-between gap-3">
          <p className="font-mono text-[10px] uppercase text-pir-text-muted">
            {final ? t.tour.finalEyebrow : `${t.tour.label} ${machine.index + 1}/${steps.length - 1}`}
          </p>
          <button
            type="button"
            onClick={onClose}
            className="h-7 w-7 rounded border border-pir text-pir-text-muted hover:text-pir-text-primary"
            aria-label={t.tour.close}
          >
            x
          </button>
        </div>

        <h2 className="mt-3 text-heading text-pir-text-primary">{stepText.title}</h2>
        <p className="mt-2 text-body leading-6 text-pir-text-secondary">{stepText.body}</p>

        {final ? (
          <div className="mt-4 flex flex-col gap-2">
            <button
              type="button"
              onClick={clearDemo}
              disabled={clearingDemo}
              className="h-9 rounded bg-pir-accent px-3 text-label font-semibold text-pir-base disabled:opacity-60"
            >
              {clearingDemo ? t.tour.clearingDemo : t.tour.clearDemo}
            </button>
            {clearError && <p className="font-mono text-caption text-pir-error">{t.tour.clearError}</p>}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setMachine(initialTourState(part));
                  setRect(null);
                }}
                className="h-8 flex-1 rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent"
              >
                {t.tour.rerun}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="h-8 flex-1 rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent"
              >
                {t.tour.keepDemo}
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-4 flex items-center justify-between gap-3">
            <div className="flex gap-1">
              {steps.slice(0, -1).map((item, index) => (
                <span
                  key={`${item.id}-${index}`}
                  className={`h-1.5 w-1.5 rounded-full ${
                    index === machine.index ? "bg-pir-accent" : "bg-pir-border-strong"
                  }`}
                  aria-hidden
                />
              ))}
            </div>
            {step.gate ? (
              <span className="font-mono text-caption text-pir-accent">{t.tour.clickToContinue}</span>
            ) : (
              <div className="flex gap-2">
                {machine.index > 0 && (
                  <button
                    type="button"
                    onClick={back}
                    className="h-8 rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent"
                  >
                    {t.tour.back}
                  </button>
                )}
                <button
                  type="button"
                  onClick={next}
                  className="h-8 rounded bg-pir-accent px-3 text-caption font-semibold text-pir-base"
                >
                  {t.tour.next}
                </button>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
