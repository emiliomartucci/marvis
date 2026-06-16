import type { IngestPendingItem } from "@/lib/types";
import { mimeShortLabel, previewKind } from "./format";

export function MimeIcon({ item, size = "default" }: {
  item: IngestPendingItem;
  size?: "default" | "large";
}) {
  const kind = previewKind(item);
  const label = mimeShortLabel(item);
  const dimension = size === "large" ? "h-12 w-10" : "h-10 w-8";
  const tone = mimeTone(kind);

  return (
    <span
      aria-hidden="true"
      className={`${dimension} relative inline-flex shrink-0 items-end justify-center rounded-sm border pb-1 font-mono text-[9px] font-bold leading-none tracking-[0.06em] ${tone}`}
    >
      <span className="absolute right-0 top-0 h-2 w-2 border-b border-l border-current bg-pir-surface-0" />
      {label}
    </span>
  );
}

function mimeTone(kind: ReturnType<typeof previewKind>): string {
  if (kind === "pdf") return "border-pir-error/50 bg-pir-error/10 text-pir-error";
  if (kind === "image") return "border-pir-success/50 bg-pir-success/10 text-pir-success";
  if (kind === "xlsx") return "border-pir-warning/50 bg-pir-warning/10 text-pir-warning";
  return "border-pir-accent/50 bg-pir-accent/10 text-pir-accent";
}
