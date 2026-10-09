// "Sound fingerprint": four drawings of the whole library, plus the same facts as plain tables.
import { useReveal } from "../../hooks/useReveal";
import { WORDS, typical } from "../../lib/themes";
import type { Profile } from "../../types/api";
import { useWorldOf } from "../common/WorldContext";
import { Orbit } from "./Orbit";
import { FP_TRAITS, Print } from "./Print";
import { Stars } from "./Stars";
import { Terrain } from "./Terrain";

export function Fingerprint({ profile }: { profile: Profile }) {
  const [headRef, headIn] = useReveal<HTMLElement>();
  return (
    <section className="fingerprint" id="fingerprint" aria-labelledby="fp-title">
      <header className={`fp-head reveal${headIn}`} ref={headRef}>
        <h2 id="fp-title"><span className="outline">Sound</span><br /><em>fingerprint</em></h2>
        <p className="lede">Every song you play, folded into one shape. No two listeners draw the same one.</p>
      </header>
      <div className="fp-grid">
        <Print traits={profile.traits} />
        <Terrain landscapes={profile.landscapes} bpm={profile.landscape_bpm} />
        <Orbit artists={profile.artists} />
        <Stars genres={profile.genres} links={profile.genre_links} />
      </div>
      <AsText profile={profile} />
    </section>
  );
}

function AsText({ profile: P }: { profile: Profile }) {
  const worldOf = useWorldOf();
  const [ref, inCls] = useReveal<HTMLDetailsElement>();
  const T = P.traits;
  return (
    <details className={`as-text reveal${inCls}`} id="fp-text" ref={ref}>
      <summary>Read the fingerprint as text</summary>
      <div>
        <table><tbody>
          <tr><th>Trait</th><th>In words</th><th>Typical</th><th>Most songs</th></tr>
          {FP_TRAITS.map(([k, n]) => (
            <tr key={k}><td>{n}</td><td>{WORDS[k](typical(k, T[k]))}</td><td>{typical(k, T[k]).toFixed(2)}</td><td>{`${T[k].p10.toFixed(2)} – ${T[k].p90.toFixed(2)}`}</td></tr>
          ))}
        </tbody></table>
        <table><tbody>
          <tr><th>World</th><th>Share</th><th>Songs</th><th>Leading artists</th></tr>
          {P.clusters.map((c) => (
            <tr key={c.id}><td>{worldOf(c.id)?.name}</td><td>{`${Math.round(c.share * 100)}%`}</td><td>{c.tracks}</td><td>{c.artists.slice(0, 4).map((a) => a.name).join(", ")}</td></tr>
          ))}
        </tbody></table>
        <table><tbody>
          <tr><th>Artist</th><th>Share of listening</th></tr>
          {P.artists.map((a) => <tr key={a.name}><td>{a.name}</td><td>{`${Math.round(a.share * 100)}%`}</td></tr>)}
        </tbody></table>
      </div>
    </details>
  );
}
