import { BrainShell } from "@/components/brain/BrainShell";
import { cookies } from "next/headers";

export default async function BrainLayout({ children }: { children: React.ReactNode }) {
  // Server-component cookie sniff to enable Recompute for operator+ users.
  // The backend remains the source of truth — UI gating is a courtesy, not security.
  const session = (await cookies()).get("pir_role")?.value ?? null;
  const canRecompute = session === "operator" || session === "admin" || session === "super_admin";
  return (
    <BrainShell userId="console-ui" canRecompute={canRecompute}>
      {children}
    </BrainShell>
  );
}
