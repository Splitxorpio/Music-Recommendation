// Progress maths for the loading screen ("the pressing plant").
import type { PipelineStatus, Progress, Stage } from "../types/api";
import { clamp } from "./util";

export type PlantStatus = Progress["status"];

/** What the screen should show. A run marked running whose process is gone reads as paused (Resume continues it). */
export function plantStatus(st: PipelineStatus, finishing: boolean): PlantStatus {
  const p = st.progress;
  if (finishing) return "done";
  if (p.status === "running" && !st.running) return "paused";
  return st.running ? "running" : p.status;
}

/** Overall fraction done, weighting each stage by how long it usually takes. */
export function overall(p: Progress, stages: Stage[]): number {
  let sum = 0, got = 0;
  for (const s of stages) {
    const st = p.stages[s.key];
    sum += s.weight;
    if (st?.state === "done") got += s.weight;
    else if (st?.state === "active") got += s.weight * (st.total ? clamp(st.done / st.total) : 0.25);
  }
  return sum ? got / sum : 0;
}

/** Cassette-counter style "012:07". */
export const fmtClock = (sec: number) => `${String(Math.floor(sec / 60)).padStart(3, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;

/** Elapsed time, plus a rough "~N min left" while running. */
export function clockText(p: Progress, status: PlantStatus, frac: number, nowSec = Date.now() / 1000): string {
  if (!p.started || status === "idle") return "000:00";
  const end = status === "running" ? nowSec : p.updated || nowSec;
  const elapsed = Math.max(0, end - p.started);
  let left = "";
  if (status === "running") left = frac > 0.04 && elapsed > 30 ? ` · ~${Math.max(1, Math.round((elapsed * (1 - frac)) / frac / 60))} min left` : " · warming up";
  return fmtClock(elapsed) + left;
}

/** Keep polling until a finished run is ready to show (errors, pauses and idle keep the loop alive, as before). */
export const keepPolling = (st: PipelineStatus) =>
  !(st.ready && !st.running && !["error", "paused", "idle"].includes(st.progress.status));
