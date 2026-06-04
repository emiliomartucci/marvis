// v1.1.0 - 2026-03-14 - Seed 4 Playwright test users via admin login + reset-token flow
// v1.1: extract session token from Set-Cookie header to bypass domain restriction on localhost
// Uses native fetch (Node 22) — no node-fetch dependency

const API_BASE = process.env.API_BASE || 'http://localhost:8100';
const ADMIN_EMAIL = process.env.TEST_ADMIN_EMAIL || 'admin@example.com';
const ADMIN_PASSWORD = process.env.TEST_ADMIN_PASSWORD || '';

const TEST_USERS = [
  { slug: 'pw-viewer',     email: 'pw-viewer@rbac.example.com',     system_role: 'viewer',      display_name: 'PW Viewer',     password: 'TestPass123!' },
  { slug: 'pw-operator',   email: 'pw-operator@rbac.example.com',   system_role: 'operator',    display_name: 'PW Operator',   password: 'TestPass123!' },
  { slug: 'pw-admin',      email: 'pw-admin@rbac.example.com',      system_role: 'admin',       display_name: 'PW Admin',      password: 'TestPass123!' },
  { slug: 'pw-superadmin', email: 'pw-superadmin@rbac.example.com', system_role: 'super_admin', display_name: 'PW SuperAdmin', password: 'TestPass123!' },
];

async function apiPost(path: string, body: unknown, session?: string): Promise<{ status: number; data: unknown }> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (session) headers['Cookie'] = `pir_session=${session}`;
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  let data: unknown;
  try { data = await res.json(); } catch { data = {}; }
  return { status: res.status, data };
}

export default async function globalSetup() {
  if (!ADMIN_PASSWORD) {
    console.warn('[global-setup] TEST_ADMIN_PASSWORD not set — skipping user seeding. Tests may fail if users do not exist.');
    return;
  }

  // Step 1: Login as admin — extract session token directly from Set-Cookie header
  // (cookie domain is .justaskmarvis.com so Playwright's cookie jar drops it for localhost)
  const loginRes = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
  });

  if (!loginRes.ok) {
    const body = await loginRes.text();
    throw new Error(`[global-setup] Admin login failed (${loginRes.status}): ${body}`);
  }

  // Extract pir_session from Set-Cookie header
  const setCookie = loginRes.headers.get('set-cookie') ?? '';
  const sessionMatch = setCookie.match(/pir_session=([^;]+)/);
  if (!sessionMatch) {
    throw new Error('[global-setup] No pir_session cookie in login response');
  }
  const session = sessionMatch[1];
  console.log('[global-setup] Admin session obtained');

  for (const user of TEST_USERS) {
    const userId = `usr_${user.slug}`;

    // Step 2: Create user (ignore 409 — already exists)
    const { status: createStatus, data: createData } = await apiPost('/api/v1/users', {
      slug: user.slug,
      email: user.email,
      display_name: user.display_name,
      system_role: user.system_role,
    }, session);

    if (createStatus !== 201 && createStatus !== 409) {
      console.warn(`[global-setup] Could not create user ${user.slug} (${createStatus}): ${JSON.stringify(createData)}`);
      continue;
    }

    // Step 3: Issue a password-reset token for the user
    const { status: tokenStatus, data: tokenData } = await apiPost(
      '/api/v1/auth/admin/issue-reset-token',
      { user_id: userId },
      session,
    );

    if (tokenStatus !== 200) {
      if (tokenStatus === 403) {
        console.log(`[global-setup] User ${user.slug} already has a password (403), skipping`);
        continue;
      }
      console.warn(`[global-setup] Could not issue reset token for ${user.slug} (${tokenStatus}): ${JSON.stringify(tokenData)}`);
      continue;
    }

    // Step 4: Set the password — helper to attempt reset with a given token
    const tryReset = async (tok: string): Promise<boolean> => {
      const { status, data } = await apiPost('/api/v1/auth/reset-password', { token: tok, new_password: user.password });
      if (status === 200) { console.log(`[global-setup] User ${user.slug} ready`); return true; }
      console.warn(`[global-setup] reset-password ${user.slug} (${status}): ${JSON.stringify(data)}`);
      return false;
    };

    const token = (tokenData as { token: string }).token;
    if (!await tryReset(token)) {
      // Retry with a fresh token (transient DB lock / bcrypt race)
      const { status: ts2, data: td2 } = await apiPost('/api/v1/auth/admin/issue-reset-token', { user_id: userId }, session);
      if (ts2 === 200) await tryReset((td2 as { token: string }).token);
    }
  }
}
