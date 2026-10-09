// Liner notes: where the numbers came from, plus the "re-dig" button that reruns the analysis.
import { useState } from "react";
import { useReveal } from "../../hooks/useReveal";
import { vars } from "../../lib/css";
import type { Profile } from "../../types/api";
import { Character } from "../common/characters";

export function Notes({ profile: P, onRedig }: { profile: Profile; onRedig: () => Promise<void> }) {
  const [ref, inCls] = useReveal<HTMLElement>();
  const [failed, setFailed] = useState(false);
  const c = P.coverage;
  const redig = () => onRedig().catch(() => setFailed(true));
  return (
    <section className={`notes reveal${inCls}`} id="notes" aria-labelledby="notes-title" ref={ref}>
      <h2 id="notes-title">Liner <em>notes</em></h2>
      <div className="notes-grid" id="notes-body">
        <div className="note"><h4>Recorded from</h4><p className="big">{c.tracks}</p><p>songs from your Spotify — top lists across three time ranges, recent plays and your saved library. Duplicate releases merged.</p></div>
        <div className="note"><h4>Measured</h4><p className="big">{c.with_traits - c.estimated}<span className="hand" style={{ fontSize: 26 }}>{` + ${c.estimated}≈`}</span></p><p>{`${c.with_traits - c.estimated} songs with official measurements; ${c.estimated} estimated from 30-second previews and calibrated against the official ones. Bass measured straight from preview audio for ${c.with_bass}.`}</p></div>
        <div className="note"><h4>How picks are scored</h4><p>Sound (fit to a world), scene (genre overlap) and connection (how strongly your own artists point there), mixed on the desk. <span className="est-mark">≈</span> estimated · <b>~</b> scene borrowed from the artists that led there.</p></div>
        <div className="note"><h4>Fine print</h4><p>{`World separation ${P.separation}: your worlds overlap a lot. Genres for ${c.artists_with_genres} artists. Sources: Spotify · ReccoBeats · Deezer · MusicBrainz.`}</p></div>
        <div className="note redig"><h4>Fresh pressing</h4><p>Re-pull your Spotify listening and dig new candidates. Anything already measured is kept, so it's quicker than the first run.</p>
          <button type="button" className="ticket" id="redig" onClick={redig}>Re-dig my library <span>{failed ? "couldn't start · try again" : "↻"}</span></button></div>
      </div>
      <div className="notes-character" id="notes-character" aria-hidden="true">
        <div style={vars({ "--c-a": "var(--lavender)", "--c-b": "var(--cream)" })}><Character name="digger" /></div>
      </div>
    </section>
  );
}
