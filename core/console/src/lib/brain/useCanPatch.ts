// Brain UX — hook that resolves whether the current user can apply/dismiss
// findings + memory operations. Source of truth is /api/v1/auth/me
// (system_role claim). Cookie-based shortcut was wrong because the JWT
// sits in pir_session, not in a separate pir_role cookie.

"use client";

import { useEffect, useState } from "react";

import { getMe } from "@/lib/api";

const OPERATOR_ROLES = new Set(["operator", "admin", "super_admin"]);

/** Returns true once /me confirms the user has operator+ role. */
export function useCanPatch(): boolean {
  const [canPatch, setCanPatch] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const me = await getMe();
        if (!active) return;
        setCanPatch(OPERATOR_ROLES.has(me.system_role));
      } catch {
        // Auth failure or network error → stay read-only.
        if (active) setCanPatch(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  return canPatch;
}
