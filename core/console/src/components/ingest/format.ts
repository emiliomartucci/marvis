import type { IngestPendingItem, IngestPendingStatus } from "@/lib/types";

export function basename(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatStatus(value: IngestPendingStatus | string): string {
  return value.replaceAll("_", " ");
}

export function fileLabel(item: IngestPendingItem): string {
  return item.target_filename ?? basename(item.file_path);
}

export function previewKind(item: IngestPendingItem): "markdown" | "pdf" | "xlsx" | "image" {
  const mime = item.mime_type ?? "";
  const source = item.file_path.toLowerCase();
  if (mime === "application/pdf" || source.endsWith(".pdf")) return "pdf";
  if (mime.startsWith("image/") || /\.(png|jpe?g|gif|webp)$/i.test(source)) return "image";
  if (
    mime.includes("spreadsheet") ||
    mime.includes("excel") ||
    /\.(xlsx|xls)$/i.test(source)
  ) {
    return "xlsx";
  }
  return "markdown";
}

export function statusTone(status: IngestPendingStatus): {
  dot: string;
  badge: string;
} {
  if (status === "awaiting_triage" || status === "parser_waiting") {
    return {
      dot: "bg-pir-warning",
      badge: "border-pir-warning/35 bg-pir-warning/10 text-pir-warning",
    };
  }
  if (status === "approved" || status === "inserted" || status === "done") {
    return {
      dot: "bg-pir-success",
      badge: "border-pir-success/35 bg-pir-success/10 text-pir-success",
    };
  }
  if (status === "parse_error" || status === "rejected") {
    return {
      dot: "bg-pir-error",
      badge: "border-pir-error/35 bg-pir-error/10 text-pir-error",
    };
  }
  return {
    dot: "bg-pir-accent",
    badge: "border-pir-accent/35 bg-pir-accent/10 text-pir-accent",
  };
}

export function mimeShortLabel(item: IngestPendingItem): string {
  const kind = previewKind(item);
  if (kind === "pdf") return "PDF";
  if (kind === "xlsx") return "XLS";
  if (kind === "image") return (item.file_path.split(".").at(-1) ?? "IMG").slice(0, 3).toUpperCase();
  return "TXT";
}
