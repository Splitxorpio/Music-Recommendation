// Boot: if nothing has been analysed yet (or a run is going), show the pressing plant; otherwise the dashboard.
import { useCallback, useEffect, useMemo, useState } from "react";
import { assignThemes, makeWorldOf } from "../lib/themes";
import { issueDate } from "../lib/util";
import { getPipeline, getProfile, startPipeline } from "../services/api";
import type { PipelineStatus, Profile } from "../types/api";
import { WorldContext } from "../components/common/WorldContext";
import { TooltipProvider } from "../components/common/Tooltip";
import { Digging } from "../components/desk/Digging";
import { Fingerprint } from "../components/fingerprint/Fingerprint";
import { Colophon, SvgDefs } from "../components/layout/Frame";
import { Masthead } from "../components/layout/Masthead";
import { Notes } from "../components/notes/Notes";
import { PressingPlant } from "../components/plant/PressingPlant";
import { Worlds } from "../components/worlds/Worlds";

type View = { kind: "booting" } | { kind: "failed" } | { kind: "plant"; st: PipelineStatus } | { kind: "dashboard"; profile: Profile };

export function App() {
  const [view, setView] = useState<View>({ kind: "booting" });

  useEffect(() => {
    let live = true;
    (async () => {
      const st = await getPipeline();
      if (!st.ready || st.running) return live && setView({ kind: "plant", st });
      const profile = await getProfile();
      if (live) setView({ kind: "dashboard", profile });
    })().catch(() => live && setView({ kind: "failed" }));
    return () => { live = false; };
  }, []);

  const redig = useCallback(async () => {
    const st = await startPipeline(true);
    setView({ kind: "plant", st });
  }, []);

  return (
    <TooltipProvider>
      <SvgDefs />
      {view.kind === "plant" ? <PressingPlant initial={view.st} />
        : view.kind === "dashboard" ? <Dashboard profile={view.profile} onRedig={redig} />
        : <Shell failed={view.kind === "failed"} />}
    </TooltipProvider>
  );
}

function Dashboard({ profile, onRedig }: { profile: Profile; onRedig: () => Promise<void> }) {
  const themeOf = useMemo(() => assignThemes(profile.clusters), [profile]);
  const worldOf = useMemo(() => makeWorldOf(themeOf), [themeOf]);
  return (
    <WorldContext.Provider value={worldOf}>
      <Masthead issue={`Issue ${issueDate(profile.meta.date)} · ${profile.meta.songs} songs`} />
      <main>
        <Digging profile={profile} />
        <Worlds clusters={profile.clusters} themeOf={themeOf} />
        <Fingerprint profile={profile} />
        <Notes profile={profile} onRedig={onRedig} />
      </main>
      <Colophon />
    </WorldContext.Provider>
  );
}

/** Before the first answer from the server: the same "pressing your records…" line the page always opened with. */
function Shell({ failed }: { failed: boolean }) {
  return (
    <main>
      <section className="hero" id="hero" aria-label="Your top recommendation">
        <div className="hero-ambient" aria-hidden="true"></div>
        <div className="hero-halftone" aria-hidden="true"></div>
        <div className="hero-inner" id="hero-inner">
          <div className="loading-line">{failed ? "can't reach the dashboard server — is dashboard.py running?" : "pressing your records…"}</div>
        </div>
      </section>
    </main>
  );
}
