import { useEffect, useState } from "react";
import { sampleColour } from "../lib/util";

/** The cover's sampled colour as "r g b" (null until it has loaded). Ignores answers for a cover no longer shown. */
export function useArtColour(url?: string | null) {
  const [rgb, setRgb] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    sampleColour(url).then((c) => { if (live && c) setRgb(c); });
    return () => { live = false; };
  }, [url]);
  return rgb;
}
