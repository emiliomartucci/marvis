"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import type { ReactNode } from "react";
import { getMe, login as apiLogin, logout as apiLogout } from "./api";
import type { AuthStatus, SystemRole } from "./types";
import { derivePermissions, type Permissions } from "./permissions";

interface AuthContextValue {
  status: AuthStatus;
  username: string | null;
  role: SystemRole | null;
  userId: string | null;
  displayName: string | null;
  permissions: Permissions;
  /** Advanced todos auto-classification is degraded to the heuristic because the
   *  gateway LLM key is missing (gh #22). Drives the honest-UX banner. */
  llmKeyMissing: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  status: "loading",
  username: null,
  role: null,
  userId: null,
  displayName: null,
  permissions: derivePermissions(null), // all-false durante loading
  llmKeyMissing: false,
  login: async () => {},
  logout: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [username, setUsername] = useState<string | null>(null);
  const [role, setRole] = useState<SystemRole | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [permissions, setPermissions] = useState<Permissions>(
    derivePermissions(null)
  );
  const [llmKeyMissing, setLlmKeyMissing] = useState(false);

  useEffect(() => {
    getMe()
      .then((user) => {
        setUsername(user.username);
        const userRole = (user.system_role ?? null) as SystemRole | null;
        setRole(userRole);
        setUserId(user.user_id ?? null);
        setDisplayName(user.display_name ?? null);
        setPermissions(derivePermissions(userRole));
        setLlmKeyMissing(user.capabilities?.todos_llm_key_missing === true);
        setStatus("authenticated");
      })
      .catch(() => {
        setStatus("unauthenticated");
      });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    await apiLogin(email, password);
    const user = await getMe();
    setUsername(user.username);
    const userRole = (user.system_role ?? null) as SystemRole | null;
    setRole(userRole);
    setUserId(user.user_id ?? null);
    setDisplayName(user.display_name ?? null);
    setPermissions(derivePermissions(userRole));
    setLlmKeyMissing(user.capabilities?.todos_llm_key_missing === true);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    await apiLogout();
    setUsername(null);
    setRole(null);
    setUserId(null);
    setDisplayName(null);
    setPermissions(derivePermissions(null));
    setLlmKeyMissing(false);
    setStatus("unauthenticated");
  }, []);

  return (
    <AuthContext.Provider
      value={{ status, username, role, userId, displayName, permissions, llmKeyMissing, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
