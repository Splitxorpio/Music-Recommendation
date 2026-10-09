// Picks 2-25 in one fixed grid: every cover has its own spot, nothing overlaps or scrolls sideways,
// and hovering never moves a neighbour. Pick 1 is the hero above.
import { useReveal } from "../../hooks/useReveal";
import { useArtColour } from "../../hooks/useArtColour";
import { vars } from "../../lib/css";
import { because, img, pad } from "../../lib/util";
import type { DigResult } from "../../hooks/useRecommendations";
import type { Pick } from "../../types/api";
import { Empty, partsLine } from "../common/Bits";
import { useTip } from "../common/Tooltip";
import { useWorldOf } from "../common/WorldContext";

export function Crate({ result }: { result: DigResult | null }) {
  const [headRef, headIn] = useReveal<HTMLElement>();
  const items = result?.items;
  return (
    <section className="crate" id="crate" aria-labelledby="crate-title">
      <header className={`crate-head reveal${headIn}`} ref={headRef}>
        <h2 id="crate-title"><em>the</em><span className="outline">Crate</span></h2>
        <p className="crate-count mono" id="crate-count">
          {!items ? "—" : items.length ? `${items.length} records pulled for you · scored on sound, scene & connection` : "empty at these settings"}
        </p>
      </header>
      <div id="crate-body">
        {items && (items.length
          ? <Grid key={result.seq} picks={items.slice(1)} />   /* new grid per dig, so the crate reshuffles in */
          : <Empty>The crate's empty. <em>Loosen a filter on the desk.</em></Empty>)}
      </div>
    </section>
  );
}

function Grid({ picks }: { picks: Pick[] }) {
  const [ref, inCls] = useReveal<HTMLOListElement>();
  return (
    <ol className={`crate-grid stagger${inCls}`} ref={ref}>
      {picks.map((r, i) => <Card key={r.id} pick={r} n={i + 2} />)}
    </ol>
  );
}

function Card({ pick: r, n }: { pick: Pick; n: number }) {
  const w = useWorldOf()(r.cluster);
  const art = r.cover_m || r.cover;
  const colour = useArtColour(art);
  const tip = useTip();
  return (
    <li>
      <a className="pick" href={r.spotify_url || "#"} target="_blank" rel="noopener" style={colour ? vars({ "--c": colour }) : undefined}
        {...tip(<><b>{r.title}</b>{r.artist}<br />{because(r)}<br />{partsLine(r.parts)}</>)}>
        <span className="art">
          <img loading="lazy" src={img(art)} alt={`Cover of ${r.album || r.title} by ${r.artist}`} />
          <span className="n mono">{pad(n)}</span><span className="score mono">{Math.round(r.score)}</span>
        </span>
        <b className="t">{r.title}</b>
        <span className="a">{r.artist}</span>
        <i className="why">{because(r)}</i>
        <span className="meta mono">
          {w && <><i className="swatch" style={{ background: w.mark }} title={w.name}></i>{w.name}</>}
          {r.tempo ? ` · ${r.tempo} bpm` : ""}
          {r.estimated && <> · <span className="est-mark">≈</span></>}
          {r.hop === 2 ? " · far" : ""}
          {r.mode === "known" ? " · familiar" : ""}
        </span>
      </a>
    </li>
  );
}
