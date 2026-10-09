// The live part of the page: the hero pick, the desk and the crate share one set of settings and results.
// Kept apart from the profile sections below so turning a dial only re-renders these three.
import { useCallback, useState } from "react";
import { useRecommendations } from "../../hooks/useRecommendations";
import type { DigSettings, Profile } from "../../types/api";
import { Crate } from "../crate/Crate";
import { Hero } from "../hero/Hero";
import { DEFAULTS, Desk } from "./Desk";

export function Digging({ profile }: { profile: Profile }) {
  const [settings, setSettings] = useState<DigSettings>(DEFAULTS);
  const [resetCount, setResetCount] = useState(0);
  const { result, busy, failed } = useRecommendations(settings);

  const change = useCallback((patch: Partial<DigSettings>) => setSettings((s) => ({ ...s, ...patch })), []);
  const reset = useCallback(() => { setSettings({ ...DEFAULTS }); setResetCount((n) => n + 1); }, []);

  const text = busy ? "re-digging…" : failed ? "couldn't reach the dashboard server" : result ? `live · ${result.count} records` : "live";

  return (
    <>
      <Hero items={result?.items ?? null} meta={profile.meta} />
      <Desk settings={settings} onChange={change} onReset={reset} resetCount={resetCount} ranges={profile.ranges} status={{ busy, text }} />
      <Crate result={result} />
    </>
  );
}
