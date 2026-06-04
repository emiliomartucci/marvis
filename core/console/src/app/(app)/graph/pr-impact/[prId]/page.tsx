// v3.0.0 - 2026-05-17 - deprecated path → redirect to /graph?lens=codex&pr=<id>
import { redirect } from "next/navigation";

export default async function Page({
  params,
}: {
  params: Promise<{ prId: string }>;
}) {
  const { prId } = await params;
  redirect(`/graph?lens=codex&pr=${encodeURIComponent(prId)}`);
}
