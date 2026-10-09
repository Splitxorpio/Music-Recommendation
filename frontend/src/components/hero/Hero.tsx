// Pick Nº 01, shown big as the issue's cover story.
import { useEffect } from "react";
import { vars } from "../../lib/css";
import { because, img, issueDate, sampleColour } from "../../lib/util";
import { useTilt } from "../../hooks/useTilt";
import type { Pick, Profile } from "../../types/api";
import { Empty, partsLine, Stickers } from "../common/Bits";
import { Character } from "../common/characters";
import { useWorldOf } from "../common/WorldContext";

interface Props { items: Pick[] | null; meta: Profile["meta"] }

export function Hero({ items, meta }: Props) {
  return (
    <section className="hero" id="hero" aria-label="Your top recommendation">
      <div className="hero-ambient" aria-hidden="true"></div>
      <div className="hero-halftone" aria-hidden="true"></div>
      <div className="hero-inner" id="hero-inner">
        {items == null ? <div className="loading-line">pressing your records…</div>
          : items.length ? <Cover pick={items[0]} total={items.length} meta={meta} />
          : <Empty style={{ gridColumn: "1/-1" }}>Nothing in the crate at these settings. <em>Open the dials up a little.</em></Empty>}
      </div>
    </section>
  );
}

const Arrow = () => (
  <svg viewBox="0 0 46 34" aria-hidden="true"><path d="M4 4 C 6 20, 18 28, 40 26 M30 18 L41 26 L31 32" fill="none" stroke="var(--acid)" strokeWidth="2.2" strokeLinecap="round" filter="url(#wobble)" /></svg>
);

function Cover({ pick: r, total, meta }: { pick: Pick; total: number; meta: Profile["meta"] }) {
  const w = useWorldOf()(r.cluster);
  const sleeve = useTilt<HTMLAnchorElement>();

  // The whole page's ambient glow takes the top pick's cover colour.
  useEffect(() => {
    let live = true;
    sampleColour(r.cover).then((rgb) => { if (live && rgb) document.documentElement.style.setProperty("--amb", rgb); });
    return () => { live = false; };
  }, [r.cover]);

  return (
    <>
      <div className="hero-kicker">
        <span className="mono">{`Issue ${issueDate(meta.date)} — Pick Nº 01 of ${total}`}</span>
        <span className="rule"></span>
        {w && <span className="sticker" style={vars({ "--r": "-3deg" }, { background: w.tint.a, color: w.tint.ink })}>{w.name}</span>}
        <Stickers pick={r} />
      </div>
      <h1 className="hero-type"><span className="l1">Your next</span><span className="l2">obsession.</span></h1>
      <div className="hero-art">
        <div className="vinyl" aria-hidden="true"></div>
        <a className="sleeve" href={r.spotify_url || "#"} target="_blank" rel="noopener" ref={sleeve}>
          <img src={img(r.cover)} alt={`Cover of ${r.album || r.title} by ${r.artist}`} />
        </a>
      </div>
      <div className="stamp" tabIndex={0} aria-label={`Match ${Math.round(r.score)} out of 100`}>
        <svg className="ring" viewBox="0 0 120 120" aria-hidden="true"><defs><path id="circ" d="M60,60 m-50,0 a50,50 0 1,1 100,0 a50,50 0 1,1 -100,0" /></defs><text><textPath href="#circ">sound · scene · connection · your match ·</textPath></text></svg>
        <b>{Math.round(r.score)}</b><small>/ 100</small>
        <span className="parts">{partsLine(r.parts, r.scene_inferred ? "~" : "")}</span>
      </div>
      <div className="hero-character" style={vars({ "--c-a": "var(--acid)", "--c-b": "var(--cream)" })}><Character name="listener" /></div>
      <div className="hero-lines">
        <p className="title">{r.title}</p>
        <p className="artist">{r.artists.join(", ") || r.artist}</p>
        <p className="because hand"><Arrow /><span>{because(r)}</span></p>
        <p className="meta mono">
          {r.tempo ? <span>{`${r.tempo} BPM`}</span> : null}
          {r.bass_word ? <span>{`${r.bass_word} bass`}</span> : null}
          {r.genres.length ? <span>{r.genres.slice(0, 2).join(" / ")}</span> : null}
          {r.album ? <span>{`from “${r.album}”`}</span> : null}
        </p>
        {r.spotify_url && <a className="ticket" href={r.spotify_url} target="_blank" rel="noopener">Play on Spotify <span>↗ admit one</span></a>}
      </div>
      <div className="barcode mono"><span>{`Vol. ${meta.songs} songs`}</span><i aria-hidden="true"></i></div>
    </>
  );
}
