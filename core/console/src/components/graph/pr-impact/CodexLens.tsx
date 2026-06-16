// v2.0.0 - 2026-05-17 - Codex lens: 3 modes (modules / module-zoom / pr-impact)
"use client";

import { useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from "react";

import {
  getCodexFunctions,
  getCodexModules,
  getPrImpact,
  listPrImpactBranches,
} from "@/lib/api";
import { LensSwitcher } from "../LensSwitcher";
import { BranchTree } from "./BranchTree";
import { CodexFunctionsCanvas } from "./CodexFunctionsCanvas";
import { CodexModulesCanvas } from "./CodexModulesCanvas";
import { PrImpactCanvas } from "./PrImpactCanvas";
import { PrImpactInspector } from "./PrImpactInspector";
import { PrImpactTopbar } from "./PrImpactTopbar";
import type {
  BranchItem,
  CodexClusterId,
  CodexFunctionItem,
  CodexModuleEdgeItem,
  CodexModuleItem,
  ModifiedFunctionItem,
  PrImpactResponse,
  TouchKind,
} from "./types";

const DEFAULT_PROJECT = "marvisx";

export function CodexLens() {
  const router = useRouter();
  const params = useSearchParams();
  const prId = useMemo(() => {
    const raw = params?.get("pr") ?? params?.get("prId");
    return raw ? decodeURIComponent(raw) : null;
  }, [params]);
  const moduleSlug = useMemo(() => params?.get("module") ?? null, [params]);

  // PR-impact state
  const [prData, setPrData] = useState<PrImpactResponse | null>(null);
  const [prError, setPrError] = useState<string | null>(null);
  const [prLoading, setPrLoading] = useState(false);

  // Modules + functions state
  const [modules, setModules] = useState<CodexModuleItem[]>([]);
  const [moduleEdges, setModuleEdges] = useState<CodexModuleEdgeItem[]>([]);
  const [modulesLoading, setModulesLoading] = useState(false);
  const [modulesError, setModulesError] = useState<string | null>(null);
  const [functions, setFunctions] = useState<CodexFunctionItem[]>([]);
  const [functionsLoading, setFunctionsLoading] = useState(false);
  const [functionsError, setFunctionsError] = useState<string | null>(null);

  // UI state
  const [depth, setDepth] = useState(1);
  const [includeAll, setIncludeAll] = useState(false);
  const [filterKinds, setFilterKinds] = useState<Set<TouchKind>>(new Set());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedFn, setSelectedFn] = useState<ModifiedFunctionItem | null>(null);
  // Single-click highlight: modulo selezionato + correlati. No URL change.
  // Double-click → onActivateModule fa mode switch a vista funzioni.
  const [selectedModuleSlug, setSelectedModuleSlug] = useState<string | null>(null);

  // Branch sidebar
  const [branches, setBranches] = useState<BranchItem[]>([]);
  const [branchState, setBranchState] = useState<"active" | "stale" | "all">(
    "active"
  );
  const [branchesError, setBranchesError] = useState<string | null>(null);
  const [branchesLoading, setBranchesLoading] = useState(true);

  // PR impact fetch
  useEffect(() => {
    if (!prId) {
      setPrData(null);
      return;
    }
    const controller = new AbortController();
    setPrLoading(true);
    getPrImpact(prId, {
      depth,
      limit: includeAll ? 200 : 50,
      include_all: includeAll,
      signal: controller.signal,
    })
      .then((resp) => {
        setPrData(resp);
        setPrError(null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== "AbortError") {
          setPrError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => setPrLoading(false));
    return () => controller.abort();
  }, [prId, depth, includeAll]);

  // Modules fetch (only when no PR is selected — macro view is the default)
  useEffect(() => {
    if (prId) return; // PR mode dominates
    const controller = new AbortController();
    setModulesLoading(true);
    getCodexModules({ project: DEFAULT_PROJECT, limit: 24, signal: controller.signal })
      .then((resp) => {
        setModules(resp.modules);
        setModuleEdges(resp.edges ?? []);
        setModulesError(null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== "AbortError") {
          setModulesError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => setModulesLoading(false));
    return () => controller.abort();
  }, [prId]);

  // Functions fetch on zoom-in
  useEffect(() => {
    if (!moduleSlug || prId) {
      setFunctions([]);
      return;
    }
    const controller = new AbortController();
    setFunctionsLoading(true);
    getCodexFunctions(moduleSlug, {
      project: DEFAULT_PROJECT,
      limit: 200,
      signal: controller.signal,
    })
      .then((resp) => {
        setFunctions(resp.functions);
        setFunctionsError(null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== "AbortError") {
          setFunctionsError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => setFunctionsLoading(false));
    return () => controller.abort();
  }, [moduleSlug, prId]);

  // Branches fetch
  useEffect(() => {
    const controller = new AbortController();
    setBranchesLoading(true);
    listPrImpactBranches({
      state: branchState,
      limit: 80,
      signal: controller.signal,
    })
      .then((resp) => {
        setBranches(resp.branches);
        setBranchesError(null);
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name !== "AbortError") {
          setBranchesError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => setBranchesLoading(false));
    return () => controller.abort();
  }, [branchState]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setSelectedNodeId(null);
        setSelectedFn(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onNodeClick = useCallback(
    (nodeId: string, fn: ModifiedFunctionItem | null) => {
      setSelectedNodeId(nodeId);
      setSelectedFn(fn);
    },
    []
  );

  const onToggleKind = useCallback((kind: TouchKind) => {
    setFilterKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  const updateUrl = useCallback(
    (mut: (sp: URLSearchParams) => void) => {
      const sp = new URLSearchParams(params?.toString() ?? "");
      sp.set("lens", "codex");
      mut(sp);
      router.push(`/graph?${sp.toString()}`);
    },
    [router, params]
  );

  const onSelectPr = useCallback(
    (newPrId: string) => {
      setSelectedNodeId(null);
      setSelectedFn(null);
      updateUrl((sp) => {
        sp.delete("module");
        sp.set("pr", newPrId);
      });
    },
    [updateUrl]
  );

  // Single-click su modulo: highlight + correlati visibili in canvas
  // (no URL change, no entry vista funzioni). Toggle se gia selezionato.
  const onSelectModule = useCallback((slug: string) => {
    setSelectedModuleSlug((prev) => (prev === slug ? null : slug));
  }, []);

  // Double-click su modulo: entry vista funzioni (era single-click pre-PR9).
  const onActivateModule = useCallback(
    (slug: string) => {
      setSelectedModuleSlug(null);
      updateUrl((sp) => {
        sp.delete("pr");
        sp.set("module", slug);
      });
    },
    [updateUrl]
  );

  const onBackToModules = useCallback(() => {
    updateUrl((sp) => {
      sp.delete("module");
      sp.delete("pr");
    });
  }, [updateUrl]);

  const onSelectFunction = useCallback((fn: CodexFunctionItem) => {
    setSelectedNodeId(fn.node_id);
    // Build a synthetic ModifiedFunctionItem shape so PrImpactInspector renders
    // even outside a PR context — only the fields it reads are populated.
    setSelectedFn({
      node_id: fn.node_id,
      qualified_name_snapshot: fn.qualified_name,
      source_file: fn.file_path ?? "",
      touch_kind: "modify",
      lines_added: 0,
      lines_removed: 0,
      weight: Math.min(1, fn.touch_count_7d * 0.2 + fn.touch_count_30d * 0.05),
      blame_author: null,
      node_missing: false,
    });
  }, []);

  // Pick the cluster of the active module (for zoom canvas color)
  const moduleCluster: CodexClusterId | null = useMemo(() => {
    if (!moduleSlug) return null;
    const m = modules.find((x) => x.slug === moduleSlug);
    return m?.cluster ?? "shared";
  }, [moduleSlug, modules]);

  // Set di slug connessi al modulo selezionato via edges (per highlight).
  // Empty se nessuna selezione → canvas usa full opacity ovunque.
  const connectedModuleSlugs = useMemo<Set<string>>(() => {
    if (!selectedModuleSlug) return new Set<string>();
    const set = new Set<string>();
    for (const e of moduleEdges) {
      if (e.source === selectedModuleSlug) set.add(e.target);
      else if (e.target === selectedModuleSlug) set.add(e.source);
    }
    return set;
  }, [selectedModuleSlug, moduleEdges]);

  const mode: "pr" | "module" | "modules" = prId
    ? "pr"
    : moduleSlug
    ? "module"
    : "modules";

  return (
    <div style={SHELL_STYLE}>
      <main style={MAIN_STYLE}>
        <CodexHeader
          mode={mode}
          moduleSlug={moduleSlug}
          prId={prId}
          onBack={onBackToModules}
        />

        {mode === "pr" && prData && (
          <PrImpactTopbar
            populatorStatus={prData.pr_metadata.populator_status}
            totalFunctions={prData.total_estimate}
            visibleFunctions={
              filterKinds.size === 0
                ? prData.modified_functions.length
                : prData.modified_functions.filter((m) =>
                    filterKinds.has(m.touch_kind)
                  ).length
            }
            capped={prData.pr_metadata.function_nodes_capped}
            capThreshold={prData.pr_metadata.function_cap_threshold}
            filterKinds={filterKinds}
            depth={depth}
            includeAll={includeAll}
            onToggleKind={onToggleKind}
            onDepthChange={setDepth}
            onToggleIncludeAll={() => setIncludeAll((v) => !v)}
          />
        )}

        <div style={CONTENT_STYLE}>
          <section style={CANVAS_AREA_STYLE}>
            {mode === "modules" && modulesLoading && <LoadingHint label="Carico moduli…" />}
            {mode === "modules" && modulesError && <ErrorBox error={modulesError} />}
            {mode === "modules" && !modulesLoading && !modulesError && (
              <CodexModulesCanvas
                modules={modules}
                edges={moduleEdges}
                project={DEFAULT_PROJECT}
                selectedSlug={selectedModuleSlug}
                connectedSlugs={connectedModuleSlugs}
                onSelect={onSelectModule}
                onActivate={onActivateModule}
              />
            )}

            {mode === "module" && functionsLoading && (
              <LoadingHint label={`Carico funzioni di ${moduleSlug}…`} />
            )}
            {mode === "module" && functionsError && <ErrorBox error={functionsError} />}
            {mode === "module" && !functionsLoading && !functionsError && moduleCluster && (
              <CodexFunctionsCanvas
                functions={functions}
                module={moduleSlug ?? ""}
                cluster={moduleCluster}
                onSelect={onSelectFunction}
                selectedNodeId={selectedNodeId}
              />
            )}

            {mode === "pr" && prLoading && <LoadingHint label="Carico impatto…" />}
            {mode === "pr" && prError && <ErrorBox error={prError} />}
            {mode === "pr" && prData && !prLoading && (
              <PrImpactCanvas
                modifiedFunctions={prData.modified_functions}
                transitiveImpact={prData.transitive_impact}
                selectedNodeId={selectedNodeId}
                filterKinds={filterKinds}
                onNodeClick={onNodeClick}
              />
            )}
          </section>

          <PrImpactInspector
            fn={selectedFn}
            onClose={() => {
              setSelectedFn(null);
              setSelectedNodeId(null);
            }}
          />
        </div>
      </main>

      {/* BranchTree floating card — stessa larghezza del LensSwitcher (~340px)
          per continuita visiva. Posizionato sotto il LensSwitcher (top:72)
          per non sovrapporsi. */}
      <div style={BRANCHES_FLOAT_STYLE}>
        <BranchTree
          branches={branches}
          selectedPrId={prId}
          onSelectPr={onSelectPr}
          state={branchState}
          onChangeState={setBranchState}
          loading={branchesLoading}
          error={branchesError}
        />
      </div>
    </div>
  );
}

function CodexHeader({
  mode,
  moduleSlug,
  prId,
  onBack,
}: {
  mode: "pr" | "module" | "modules";
  moduleSlug: string | null;
  prId: string | null;
  onBack: () => void;
}) {
  return (
    <header style={HEADER_STYLE}>
      <LensSwitcher active="codex" />
      <div style={CRUMB_SEP_STYLE}>·</div>
      <div style={CRUMBS_STYLE}>
        <button onClick={onBack} style={CRUMB_BTN_STYLE}>
          Codex · moduli
        </button>
        {mode === "module" && moduleSlug && (
          <>
            <span style={CRUMB_SEP_STYLE}>›</span>
            <span style={CRUMB_ACTIVE_STYLE}>{moduleSlug}</span>
          </>
        )}
        {mode === "pr" && prId && (
          <>
            <span style={CRUMB_SEP_STYLE}>›</span>
            <span style={CRUMB_ACTIVE_STYLE}>PR {prId.replace(/^pr:artifact:/, "").slice(0, 12)}…</span>
          </>
        )}
      </div>
    </header>
  );
}

function LoadingHint({ label }: { label: string }) {
  return <p style={LOADING_STYLE}>{label}</p>;
}

function ErrorBox({ error }: { error: string }) {
  return (
    <div style={ERROR_STYLE}>
      <p style={{ fontWeight: 600 }}>Caricamento fallito</p>
      <p
        style={{
          marginTop: 6,
          fontFamily: "var(--pir-font-mono, monospace)",
          fontSize: 11,
        }}
      >
        {error}
      </p>
    </div>
  );
}

const SHELL_STYLE: CSSProperties = {
  position: "relative",
  flex: 1,
  minHeight: 0,
  background: "hsl(var(--pir-base))",
  color: "var(--pir-text-primary)",
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
  overflow: "hidden",
};

const MAIN_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

const BRANCHES_FLOAT_STYLE: CSSProperties = {
  position: "absolute",
  top: 88,
  left: 12,
  width: 340,
  maxHeight: "calc(100% - 100px)",
  zIndex: 9,
  display: "flex",
  flexDirection: "column",
  background: "hsl(var(--pir-surface-0) / 0.96)",
  backdropFilter: "blur(6px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  boxShadow: "0 8px 24px hsl(0 0% 0% / 0.18)",
  overflow: "hidden",
};

const HEADER_STYLE: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "8px 16px",
  borderBottom: "1px solid var(--pir-border)",
  background: "hsl(var(--pir-surface-0))",
};

const CRUMBS_STYLE: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: 12,
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-mono, monospace)",
};

const CRUMB_BTN_STYLE: CSSProperties = {
  background: "transparent",
  border: "none",
  padding: 0,
  cursor: "pointer",
  color: "var(--pir-text-tertiary)",
  fontFamily: "inherit",
  fontSize: 12,
  textTransform: "uppercase",
  letterSpacing: "0.12em",
  fontWeight: 600,
};

const CRUMB_SEP_STYLE: CSSProperties = {
  color: "var(--pir-text-muted)",
};

const CRUMB_ACTIVE_STYLE: CSSProperties = {
  color: "var(--pir-text-primary)",
};

const CONTENT_STYLE: CSSProperties = {
  display: "flex",
  flex: 1,
  overflow: "hidden",
};

const CANVAS_AREA_STYLE: CSSProperties = {
  position: "relative",
  flex: 1,
  overflow: "hidden",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

const LOADING_STYLE: CSSProperties = {
  fontSize: 12,
  color: "var(--pir-text-tertiary)",
};

const ERROR_STYLE: CSSProperties = {
  maxWidth: 480,
  padding: 16,
  border: "1px solid hsl(var(--pir-error) / 0.4)",
  background: "hsl(var(--pir-error) / 0.1)",
  borderRadius: 2,
  color: "hsl(var(--pir-error))",
  fontSize: 13,
};
