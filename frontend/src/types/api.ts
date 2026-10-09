// Shapes of what dashboard.py returns. See Data.build_profile / Data.recommendations / pipeline_status there.

export interface TraitStats { mean: number; p10: number; p50: number; p90: number }
export type TraitKey =
  | "tempo" | "tempo_feel" | "energy" | "danceability" | "valence" | "acousticness"
  | "speechiness" | "instrumentalness" | "loudness" | "low_end_share";
export type Traits = Partial<Record<TraitKey, TraitStats>>;

export interface ArtistShare { name: string; share: number; photo?: string | null }

export interface ClusterExample {
  id: string; title: string | null; artists: string[];
  cover?: string | null; cover_m?: string | null; estimated: boolean; spotify_url: string;
}

export interface Cluster {
  id: number; label: string; share: number; tracks: number; estimated_share: number;
  traits: Traits; artists: ArtistShare[]; genres: [string, number][]; examples: ClusterExample[];
}

export interface Profile {
  meta: { date: string; songs: number };
  coverage: { tracks: number; with_traits: number; estimated: number; with_bass: number; artists_with_genres: number };
  traits: Required<Traits>;
  genres: [string, number][];
  artists: ArtistShare[];
  clusters: Cluster[];
  landscapes: { cluster: number; points: number[] }[];
  landscape_bpm: [number, number];
  genre_links: { a: string; b: string; w: number }[];
  library_covers: { cover: string; cover_m?: string | null; title: string; artist: string }[];
  ranges: { bpm: [number, number]; bass: [number, number] };
  default_weights: { sound: number; scene: number; connection: number };
  separation: number | null;
}

export interface Pick {
  id: string; title: string; artist: string; artists: string[]; album?: string | null;
  cover?: string | null; cover_m?: string | null;
  score: number; parts: { sound: number | null; scene: number | null; connection: number | null };
  cluster: number; reason: string; tempo: number | null; bass: number | null; bass_word: string | null;
  estimated: boolean; scene_inferred: boolean; mode?: string; hop?: number; release?: string | null;
  genres: string[]; spotify_url?: string | null;
}

export interface Recommendations { count: number; items: Pick[] }

export interface Stage {
  key: string; side: string; title: string; lines: [string, string]; weight: number; blurb: string;
}

export interface StageProgress {
  state: "pending" | "active" | "done"; done: number; total: number | null; label: string | null;
}

export interface Cover { url: string; title: string; artist: string; tag?: string | null }

export interface Progress {
  status: "idle" | "running" | "done" | "error" | "paused";
  stage: string | null; now: string | null; error: string | null;
  started: number | null; updated: number | null; pid?: number | null;
  covers: Cover[]; facts: { text: string; key?: string | null }[];
  stages: Record<string, StageProgress>;
}

export interface PipelineStatus { progress: Progress; stages: Stage[]; running: boolean; ready: boolean }

/** What the desk sends: weights 0-100 as on the faders, ranges null when left wide open. */
export interface DigSettings {
  ws: number; wc: number; wn: number; known: boolean; far: boolean;
  bpm: [number, number] | null; bass: [number, number] | null;
}
