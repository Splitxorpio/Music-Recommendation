// "Your worlds": one themed poster per sub-taste, with its traits, cover collage, artist faces and character.
import { useReveal } from "../../hooks/useReveal";
import { vars } from "../../lib/css";
import { THEMES, WORDS, type ThemeKey, type WordKey } from "../../lib/themes";
import { img, pad } from "../../lib/util";
import type { Cluster } from "../../types/api";
import { Character } from "../common/characters";
import { useTip } from "../common/Tooltip";
import { Motif } from "./Motif";

const COVER_SPOTS = [
  { l: 4, t: 6, w: 46, r: -6, d: 0 }, { l: 54, t: 0, w: 32, r: 5, d: 1.2 }, { l: 42, t: 42, w: 40, r: -3, d: 2.4 },
  { l: 0, t: 60, w: 30, r: 7, d: 0.8 }, { l: 76, t: 50, w: 22, r: -8, d: 3 }, { l: 24, t: 36, w: 18, r: 4, d: 1.8 },
];
const FACE_SPOTS = [{ l: 78, t: 14, w: 17 }, { l: 30, t: 78, w: 15 }, { l: 86, t: 82, w: 12 }];
const CHAR_SPOT: Record<ThemeKey, { left: string; top: string }> = {
  night: { left: "58%", top: "70%" }, volume: { left: "64%", top: "-2%" }, lowend: { left: "62%", top: "72%" }, soft: { left: "6%", top: "-8%" },
};
const TRAIT_ROWS: [WordKey, string][] = [["tempo_feel", "tempo"], ["energy", "energy"], ["valence", "mood"], ["low_end_share", "bass"], ["speechiness", "vocals"]];
const TILT = [-2, 1.5, -1, 2, -1.5];

export function Worlds({ clusters, themeOf }: { clusters: Cluster[]; themeOf: Record<number, ThemeKey> }) {
  const [headRef, headIn] = useReveal<HTMLElement>();
  return (
    <section className="worlds" id="worlds" aria-labelledby="worlds-title">
      <header className={`worlds-head reveal${headIn}`} ref={headRef}>
        <h2 id="worlds-title">Your <em>worlds</em></h2>
        <p className="lede" id="worlds-lede">{`${clusters.length} sides of how you listen, found by grouping your songs on how they sound. They blur into each other at the edges — more spectrum than boxes.`}</p>
      </header>
      <div id="worlds-body">
        {clusters.map((c, i) => <World key={c.id} c={c} i={i} n={clusters.length} theme={themeOf[c.id]} />)}
      </div>
    </section>
  );
}

function World({ c, i, n, theme }: { c: Cluster; i: number; n: number; theme: ThemeKey }) {
  const t = THEMES[theme], tr = c.traits;
  const [copyRef, copyIn] = useReveal<HTMLDivElement>();
  const [collageRef, collageIn] = useReveal<HTMLDivElement>();
  const tip = useTip();
  const tint = vars({ "--c-a": t.tint.a, "--c-b": t.tint.b, "--c-ink": t.tint.ink, "--c-line": t.tint.line }, CHAR_SPOT[theme]);

  return (
    <article className={`world${i % 2 ? " flip" : ""}`} data-theme={theme} aria-labelledby={`w${c.id}`}>
      <div className="motif"><Motif theme={theme} id={c.id} /></div>
      <div className={`copy reveal${copyIn}`} ref={copyRef}>
        <div className="kicker mono"><span>{`World ${pad(i + 1)} / ${pad(n)}`}</span><span>{`— ${t.kicker}`}</span></div>
        <p className="share outline" aria-label={`${Math.round(c.share * 100)} percent of your listening`}>{Math.round(c.share * 100)}<small>%</small></p>
        <h3 id={`w${c.id}`}>{t.lines[0]}<em>{t.lines[1]}</em></h3>
        <p className="auto">{`of your listening · ${c.tracks} songs · ${Math.round(c.estimated_share * 100)}% estimated from clips`}<br />{`measured as: ${c.label}`}</p>
        <ul className="traits">
          {TRAIT_ROWS.filter(([k]) => tr[k]).map(([k, label], j) => {
            const v = k === "tempo_feel" ? tr[k]!.p50 : tr[k]!.mean;
            return <li key={k} style={vars({ "--r": `${TILT[j]}deg` })}><b>{WORDS[k](v)}</b><span>{`${label} ${k === "tempo_feel" ? "" : v.toFixed(2)}`}</span></li>;
          })}
        </ul>
        <p className="genres">{c.genres.slice(0, 6).map((g) => g[0]).join(" / ")}</p>
        <p className="who">{c.artists.map((a) => a.name).join(" · ")}</p>
      </div>
      <div className={`collage stagger${collageIn}`} ref={collageRef}>
        {c.examples.filter((e) => e.cover).slice(0, 6).map((e, j) => {
          const s = COVER_SPOTS[j];
          return (
            <a key={e.id} className="piece" href={e.spotify_url} target="_blank" rel="noopener"
              style={vars({ "--r": `${s.r}deg`, "--d": `${s.d}s`, "--t": `${9 + j}s` }, { left: `${s.l}%`, top: `${s.t}%`, width: `${s.w}%`, aspectRatio: 1, zIndex: 6 - j })}>
              <img loading="lazy" src={img(e.cover)} alt={`Cover: ${e.title} by ${e.artists[0] || ""}`} />
              <span className="lbl">{`${e.title} — ${e.artists[0] || ""}${e.estimated ? " ≈" : ""}`}</span>
            </a>
          );
        })}
        {c.artists.filter((a) => a.photo).slice(0, 3).map((a, j) => {
          const s = FACE_SPOTS[j];
          return (
            <div key={a.name} className="face" style={vars({ "--d": `${j}s` }, { left: `${s.l}%`, top: `${s.t}%`, width: `${s.w}%`, aspectRatio: 1, zIndex: 7 + j })}
              {...tip(<><b>{a.name}</b>{Math.round(a.share * 100)}% of this world</>)}>
              <img loading="lazy" src={img(a.photo)} alt="" /><span className="nm">{a.name}</span>
            </div>
          );
        })}
        <div className="character" style={tint}><Character name={t.char} /></div>
      </div>
    </article>
  );
}
