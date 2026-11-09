export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";
const ACCESS_KEY = "bidready.access";
const REFRESH_KEY = "bidready.refresh";

export function token() {
  return typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY);
}

export function clearSession() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export async function login(email: string, password: string) {
  const response = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const payload = await decode(response);
  localStorage.setItem(ACCESS_KEY, payload.access_token);
  localStorage.setItem(REFRESH_KEY, payload.refresh_token);
  return payload;
}

function refreshToken() {
  return typeof window === "undefined" ? null : localStorage.getItem(REFRESH_KEY);
}

/**
 * One refresh at a time.
 *
 * A screen fires several queries at once, so an expired access token produces a
 * burst of simultaneous 401s. Refreshing per request would spend the refresh
 * token several times over — and the server issues a new one on every call, so
 * the last writer wins and the others are left holding tokens that were already
 * replaced. Every caller therefore awaits the same in-flight promise.
 */
let inFlightRefresh: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  const refresh = refreshToken();
  if (!refresh) return false;
  if (!inFlightRefresh) {
    inFlightRefresh = (async () => {
      try {
        const response = await fetch(`${API_URL}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (!response.ok) return false;
        const payload = await response.json();
        localStorage.setItem(ACCESS_KEY, payload.access_token);
        localStorage.setItem(REFRESH_KEY, payload.refresh_token);
        return true;
      } catch {
        return false;
      } finally {
        // Cleared on the next tick so callers that arrived during this refresh
        // still observe its result rather than starting another one.
        setTimeout(() => {
          inFlightRefresh = null;
        }, 0);
      }
    })();
  }
  return inFlightRefresh;
}

async function send(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers);
  const access = token();
  if (access) headers.set("Authorization", `Bearer ${access}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  return fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  let response = await send(path, init);

  // The access token lives 30 minutes and the refresh token 14 days. Treating a
  // 401 as "signed out" threw away a session that was still perfectly valid and
  // dropped the user back at the login screen mid-task — while a working
  // /auth/refresh endpoint sat unused and the refresh token sat in storage.
  //
  // A FormData body is a one-shot stream: it cannot be replayed after being
  // consumed, so an upload that meets an expired token is not retried here. It
  // surfaces as an error and the next attempt succeeds on the fresh token.
  if (response.status === 401 && !(init.body instanceof FormData)) {
    if (await refreshSession()) {
      response = await send(path, init);
    }
  }
  if (response.status === 401) clearSession();
  return decode(response) as Promise<T>;
}

/**
 * Drive one 202-then-stream copilot answer.
 *
 * `EventSource` cannot carry an Authorization header, so the SSE stream is read
 * from `fetch` directly. The answer is committed server-side before the job
 * completes, so a dropped stream costs the live update and never the message.
 */
export async function streamJob(
  streamUrl: string,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const headers = new Headers();
  const access = token();
  if (access) headers.set("Authorization", `Bearer ${access}`);
  const base = API_URL.replace(/\/api\/v1$/, "");
  const response = await fetch(`${base}${streamUrl}`, { headers, cache: "no-store" });
  if (!response.ok || !response.body) throw new Error(`Stream failed (${response.status})`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let name = "message";
      let payload = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7).trim();
        else if (line.startsWith("data: ")) payload += line.slice(6);
      }
      if (!payload) continue;
      try { onEvent(name, JSON.parse(payload)); } catch { /* keep-alive or partial frame */ }
    }
  }
}

type FieldError = { loc?: (string | number)[]; msg?: string };

/** Turn FastAPI's `errors` array into something a person can act on.
 *  The backend already names the offending field and says why; showing only
 *  "One or more fields failed validation." throws that away. */
function describeFieldErrors(errors: FieldError[]): string {
  const parts = errors.slice(0, 4).map((error) => {
    const path = (error.loc ?? []).filter((part) => part !== "body" && typeof part === "string");
    const field = path.length ? path.join(".") : "request";
    return `${field}: ${error.msg ?? "invalid"}`;
  });
  const more = errors.length > parts.length ? ` (+${errors.length - parts.length} more)` : "";
  return parts.join("; ") + more;
}

async function decode(response: Response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (Array.isArray(payload.errors) && payload.errors.length > 0) {
      throw new Error(describeFieldErrors(payload.errors as FieldError[]));
    }
    throw new Error(payload.detail?.message ?? payload.detail ?? `Request failed (${response.status})`);
  }
  return payload;
}
