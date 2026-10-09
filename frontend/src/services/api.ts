// Every request the page makes to the local dashboard server (dashboard.py). Same endpoints, methods and
// query parameters as before the React move; nothing here talks to outside services or holds credentials.
import type { DigSettings, PipelineStatus, Profile, Recommendations } from "../types/api";

async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${init?.method ?? "GET"} ${url} → ${r.status}`);
  return r.json() as Promise<T>;
}

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
