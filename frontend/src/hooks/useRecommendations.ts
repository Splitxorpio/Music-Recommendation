import { useEffect, useRef, useState } from "react";
import { getRecommendations } from "../services/api";
import type { DigSettings, Pick } from "../types/api";

export interface DigResult { items: Pick[]; count: number; seq: number }

/**
 * Live picks for the desk's settings. The first load goes out at once; later changes wait 220 ms for the
 * dials to settle, and a newer change cancels an older request so only the latest settings ever render.
 */
export function useRecommendations(settings: DigSettings) {
  const [result, setResult] = useState<DigResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const first = useRef(true);

  useEffect(() => {
    const ctrl = new AbortController();
    const delay = first.current ? 0 : 220;
    first.current = false;
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await getRecommendations(settings, ctrl.signal);
        setResult((prev) => ({ items: r.items, count: r.count, seq: (prev?.seq ?? 0) + 1 }));
        setFailed(false);
        setBusy(false);
      } catch {
        if (ctrl.signal.aborted) return;            // superseded by a newer setting
        setFailed(true);
        setBusy(false);
      }
    }, delay);
    return () => { clearTimeout(timer); ctrl.abort(); };
  }, [settings]);

  return { result, busy, failed };
}
