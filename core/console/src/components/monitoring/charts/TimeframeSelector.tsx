"use client";

interface Props {
  value: "24h" | "7d" | "30d";
  onChange: (v: "24h" | "7d" | "30d") => void;
}

const OPTIONS: { value: "24h" | "7d" | "30d"; label: string }[] = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

export default function TimeframeSelector({ value, onChange }: Props) {
  return (
    <div className="flex gap-1">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`px-2 py-0.5 text-caption rounded transition-colors ${
            value === opt.value
              ? "border border-pir-accent text-pir-accent"
              : "border border-pir text-pir-text-muted hover:text-pir-text-secondary"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
