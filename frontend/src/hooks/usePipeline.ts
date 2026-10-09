import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { keepPolling } from "../lib/progress";
import { getPipeline, startPipeline } from "../services/api";
import type { PipelineStatus } from "../types/api";

/**
 * Follows the analysis run: polls /api/pipeline once a second while there's something to watch, and
 * starts/resumes it on request. `onStatus` sees every fresh status (the plant uses it to collect covers).
 */
export function usePipeline(initial: PipelineStatus, onStatus: (st: PipelineStatus) => void) {
  const [st, setSt] = useState(initial);
  // The next poll: null = stopped. `n` makes every scheduled poll a new value, so the effect re-runs.
  const [next, setNext] = useState<{ delay: number; n: number } | null>(
    initial.running || initial.progress.status === "running" ? { delay: 1000, n: 0 } : null);
  const stopped = useRef(false);
  const handle = useRef(onStatus);
  useLayoutEffect(() => { handle.current = onStatus; });

  const take = useCallback((s: PipelineStatus) => { setSt(s); handle.current(s); }, []);

  useEffect(() => {
    if (!next) return;
    let live = true;
    const t = setTimeout(async () => {
      let again = true;
      try {
        const s = await getPipeline();
        if (!live) return;
        take(s);
        again = keepPolling(s);
      } catch { /* server restarting; keep trying */ }
      if (live && !stopped.current) setNext(again ? { delay: 1000, n: next.n + 1 } : null);
    }, next.delay);
    return () => { live = false; clearTimeout(t); };
  }, [next, take]);

  const start = useCallback(async () => {
    const s = await startPipeline();
    take(s);
    setNext((prev) => ({ delay: 800, n: (prev?.n ?? 0) + 1 }));
  }, [take]);

  const stop = useCallback(() => { stopped.current = true; setNext(null); }, []);

  return { st, start, stop };
}
