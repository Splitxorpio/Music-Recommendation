// Every request the page makes. The first four endpoints are the same as with the single-user dashboard.py;
// the multi-user server (frequency/api) answers them for the signed-in user and adds the account calls below.
// Nothing here talks to outside services or holds credentials (the session lives in an HttpOnly cookie).
import type { Connections, DigSettings, PipelineStatus, Profile, Recommendations, SessionInfo } from "../types/api";

/** The server wants a signed-in user (multi-user server only; dashboard.py never says this). */
export class AuthRequired extends Error {}

/** A refusal with a message meant for the person (e.g. "That doesn't look like an email address."). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

/** The CSRF token the server put in a readable cookie; echoed in a header on every state-changing request. */
export function csrfToken(cookie: string = typeof document === "undefined" ? "" : document.cookie): string {
  const m = cookie.match(/(?:^|;\s*)freq_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const headers = new Headers(init?.headers);
  if (method !== "GET") headers.set("X-CSRF-Token", csrfToken());
  const r = await fetch(url, { ...init, headers, credentials: "same-origin" });
  if (r.status === 401 && !url.startsWith("/api/v1/auth/")) throw new AuthRequired();
  if (!r.ok) {
    let msg = `${method} ${url} → ${r.status}`;
    try { msg = (await r.json()).error ?? msg; } catch { /* not JSON */ }
    throw new ApiError(msg, r.status);
  }
  return (r.status === 204 ? undefined : r.json()) as Promise<T>;
}

const postJSON = <T>(url: string, body: unknown, method = "POST") =>
  getJSON<T>(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const getPipeline = () => getJSON<PipelineStatus>("/api/pipeline");

export const startPipeline = (refresh = false) =>
  getJSON<PipelineStatus>(`/api/pipeline/start${refresh ? "?refresh=1" : ""}`, { method: "POST" });

export const getProfile = () => getJSON<Profile>("/api/profile");

export function recommendationQuery(s: DigSettings, n = 25): string {
  const q = new URLSearchParams({
    ws: String(s.ws / 100), wc: String(s.wc / 100), wn: String(s.wn / 100),
    known: s.known ? "1" : "0", far: s.far ? "1" : "0", n: String(n),
  });
  if (s.bpm) q.set("bpm", s.bpm.join(","));
  if (s.bass) q.set("bass", s.bass.join(","));
  return q.toString();
}

export const getRecommendations = (s: DigSettings, signal?: AbortSignal) =>
  getJSON<Recommendations>(`/api/recommendations?${recommendationQuery(s)}`, { signal });

// ── accounts and listening sources (multi-user server) ──

export const signIn = (email: string, password: string) => postJSON<SessionInfo>("/api/v1/auth/login", { email, password });

export const signUp = (email: string, password: string, display_name?: string) =>
  postJSON<SessionInfo>("/api/v1/auth/signup", { email, password, display_name });

export const signOut = () => getJSON<void>("/api/v1/auth/logout", { method: "POST" });

export const getConnections = () => getJSON<Connections>("/api/v1/connections");

/** Sends the browser to Spotify's consent page; Spotify sends it back to the server, which starts a run. */
export async function connectSpotify() {
  const { url } = await getJSON<{ url: string }>("/api/v1/connections/spotify/authorize", { method: "POST" });
  window.location.assign(url);
}

export const connectLastfm = (username: string) => postJSON<unknown>("/api/v1/connections/lastfm", { username });

export const startRun = (source?: "spotify" | "lastfm") => postJSON<unknown>("/api/v1/runs", { source });

export function uploadHistory(file: File) {
  const body = new FormData();
  body.append("file", file);
  return getJSON<unknown>("/api/v1/uploads/history", { method: "POST", body });
}

export const setConsent = (calibration_consent: boolean) => postJSON<unknown>("/api/v1/me", { calibration_consent }, "PATCH");

export const deleteAccount = (password: string) => postJSON<void>("/api/v1/me/delete", { password });
