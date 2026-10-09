// Each sub-taste gets a visual universe ("world"), chosen from the words in its auto-label.
import type { CastName } from "../components/common/characters";
import type { Cluster, TraitKey } from "../types/api";

export type ThemeKey = "night" | "volume" | "lowend" | "soft";

export interface Theme {
  words: Record<string, number>; lines: [string, string]; kicker: string; poster: string; mark: string;
  char: CastName; tint: { a: string; b: string; ink: string; line: string };
}

export const THEMES: Record<ThemeKey, Theme> = {
  night:  { words: { dark: 3, slow: 2, quieter: 1, "laid-back": 1, "less danceable": 1 }, lines: ["After", "dark"], kicker: "the 2 a.m. side", poster: "#8fb0ff", mark: "#4a78ff", char: "rooftop", tint: { a: "#8fb0ff", b: "#e6ecff", ink: "#05091c", line: "#e6ecff" } },
  volume: { words: { upbeat: 3, "high-energy": 3, loud: 2, fast: 2, danceable: 2 }, lines: ["Volume", "up"], kicker: "the loud side", poster: "#ff9a5c", mark: "#e66a2a", char: "dancer", tint: { a: "#ff8a4c", b: "#ffeee3", ink: "#1a0b05", line: "#ffeee3" } },
  lowend: { words: { "bass-heavy": 3, "rap-heavy": 3, quieter: 1, slow: 1 }, lines: ["Low end", "theory"], kicker: "the heavy side", poster: "#c6f432", mark: "#11917f", char: "walker", tint: { a: "#c6f432", b: "#e3f5ee", ink: "#051210", line: "#e3f5ee" } },
  soft:   { words: { acoustic: 3, "light on bass": 2, melodic: 2, "laid-back": 2, vocal: 1, quieter: 1 }, lines: ["Soft", "focus"], kicker: "the tender side", poster: "#d9b8ff", mark: "#c070c8", char: "dreamer", tint: { a: "#8a52a8", b: "#ffffff", ink: "#241a33", line: "#241a33" } },
};

/** Best-scoring theme per cluster, each theme used at most once. Earlier label words count more. */
export function assignThemes(clusters: Pick<Cluster, "id" | "label">[]): Record<number, ThemeKey> {
  const pairs: { id: number; key: ThemeKey; score: number }[] = [];
  for (const c of clusters) {
    const parts = c.label.split(",").map((s) => s.trim());
    for (const [key, t] of Object.entries(THEMES) as [ThemeKey, Theme][]) {
      const score = parts.reduce((acc, p, i) => acc + (t.words[p] || 0) * (3 - i), 0);
      pairs.push({ id: c.id, key, score });
    }
  }
  pairs.sort((a, b) => b.score - a.score);
  const out: Record<number, ThemeKey> = {}, used = new Set<ThemeKey>();
  for (const p of pairs) if (!(p.id in out) && !used.has(p.key)) { out[p.id] = p.key; used.add(p.key); }
  return out;
}

export interface World extends Theme { key: ThemeKey; name: string }
export type WorldOf = (cid: number) => World | null;

export function makeWorldOf(themeOf: Record<number, ThemeKey>): WorldOf {
  return (cid) => {
    const key = themeOf[cid];
    return key ? { key, ...THEMES[key], name: `${THEMES[key].lines[0]} ${THEMES[key].lines[1]}` } : null;
  };
}

/* ---------------------------------------------------------------- plain words for traits */
export type WordKey = "energy" | "valence" | "low_end_share" | "speechiness" | "danceability" | "acousticness" | "tempo_feel" | "loudness";
export const WORDS: Record<WordKey, (v: number) => string> = {
  energy: (v) => (v < 0.5 ? "calm" : v < 0.63 ? "steady" : v < 0.74 ? "charged" : "full-tilt"),
  valence: (v) => (v < 0.3 ? "dark" : v < 0.45 ? "moody" : v < 0.6 ? "bright-ish" : "sunny"),
  low_end_share: (v) => (v < 0.65 ? "light" : v < 0.78 ? "solid" : "heavy"),
  speechiness: (v) => (v < 0.08 ? "sung" : v < 0.16 ? "half-rapped" : "rapped"),
  danceability: (v) => (v < 0.55 ? "loose" : v < 0.7 ? "nodding" : "dancey"),
  acousticness: (v) => (v < 0.1 ? "all machine" : v < 0.3 ? "mostly produced" : "organic"),
  tempo_feel: (v) => `~${Math.round(v)} bpm`,
  loudness: (v) => (v < -9 ? "hushed" : v < -6.5 ? "present" : "loud"),
};

/** Tempo is described by its median, everything else by its mean. */
export const typical = (k: TraitKey, s: { mean: number; p50: number }) => (k === "tempo_feel" ? s.p50 : s.mean);
