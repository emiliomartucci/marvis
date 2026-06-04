// v3.0.0 - 2026-05-17 - deprecated path → redirect to unified /graph?lens=codex
import { redirect } from "next/navigation";

export default function Page() {
  redirect("/graph?lens=codex");
}
