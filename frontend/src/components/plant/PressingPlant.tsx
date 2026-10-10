// The pressing plant: the loading screen shown while the library is analysed.
// A record store after hours. A turntable plays the run, the sleeve-back tracklist shows each stage,
// and real covers drop into the crate as they're found.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { usePipeline } from "../../hooks/usePipeline";
import { vars } from "../../lib/css";
import { clockText, overall, plantStatus, type PlantStatus } from "../../lib/progress";
import { clamp, img, REDUCED, sampleColour } from "../../lib/util";
import type { Cover, PipelineStatus, Stage } from "../../types/api";
import { Character, type CastName } from "../common/characters";

const CHAR_FOR: Record<string, CastName> = { login: "listener", pull: "digger", notes: "digger", previews: "listener", lowend: "walker",
  calibrate: "dreamer", genres: "digger", fingerprint: "dancer", crates: "digger", sleeves: "dancer" };
const BULLETS = ["★", "→", "✶", "♪", "✳", "~"];
const FACT_TILT = [-1.5, 1, -0.5, 1.5, -1, 0.5, -2, 1];
const MAX_CRATE = 26;

interface CrateCover extends Cover { r: string }

export function PressingPlant({ initial }: { initial: PipelineStatus }) {
  // Covers ever seen (the crate shows the newest MAX_CRATE, each with its own random tilt).
  const seen = useRef(new Set<string>());
  const [crate, setCrate] = useState<CrateCover[]>([]);
  const [seenCount, setSeenCount] = useState(0);
  const [finishing, setFinishing] = useState(false);
  const [starting, setStarting] = useState(false);

  const collect = useCallback((s: PipelineStatus) => {
    const fresh = s.progress.covers.filter((c) => !seen.current.has(c.url));
    if (!fresh.length) return;
    for (const c of fresh) seen.current.add(c.url);
    const tilted = fresh.map((c) => ({ ...c, r: `${(Math.random() * 14 - 7).toFixed(1)}deg` }));
    setCrate((prev) => [...prev, ...tilted].slice(-MAX_CRATE));
    setSeenCount(seen.current.size);
  }, []);

  const { st, start, stop } = usePipeline(initial, collect);
  useEffect(() => collect(initial), [collect, initial]);

  const p = st.progress, stages = st.stages;
  const [broken, setBroken] = useState<Set<string>>(() => new Set());   // covers that failed to load leave the crate
  const status = plantStatus(st, finishing);
  const frac = status === "done" ? 1 : overall(p, stages);
  const playing = status === "running";

  // Covering the page: no scrolling behind the plant, and start at the top.
  useLayoutEffect(() => {
    document.body.classList.add("pressing");
    scrollTo(0, 0);
    return () => document.body.classList.remove("pressing");
  }, []);

  // Done: the record lifts off, the room fades, then the page reloads into the dashboard.
  useEffect(() => {
    if (status !== "done" || finishing) return;
    setFinishing(true);
    stop();
  }, [status, finishing, stop]);
  useEffect(() => {
    if (!finishing) return;
    const t = setTimeout(() => location.reload(), REDUCED ? 300 : 2200);
    return () => clearTimeout(t);
  }, [finishing]);

  // The newest cover tints the record label and the room.
  const latest = p.covers[p.covers.length - 1]?.url;
  const [amb, setAmb] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    sampleColour(latest).then((rgb) => { if (live && rgb) setAmb(rgb); });
    return () => { live = false; };
  }, [latest]);

  const go = async () => {
    setStarting(true);
    try { await start(); } finally { setStarting(false); }
  };

  const ast = p.stage ? p.stages[p.stage] : null;
  const want: CastName = status === "idle" || status === "paused" || status === "error" ? "listener" : status === "done" ? "dancer" : CHAR_FOR[p.stage ?? ""] || "digger";
  const facts = [...p.facts].reverse().slice(0, 6);

  return (
    <section className={`plant${finishing ? " lifting" : ""}`} id="plant" aria-live="polite" data-status={status} style={amb ? vars({ "--amb": amb }) : undefined}>
      <div className="plant-amb" aria-hidden="true"></div>
      <header className="plant-top">
        <span className="plant-mark">Frequency</span>
        <span className="mono plant-sub">the pressing plant · open after hours</span>
        <span className="mono plant-clock" id="pl-clock" aria-label="Elapsed time">{clockText(p, status, frac)}</span>
      </header>
      <div className="plant-grid">
        <div className="plant-copy">
          <Copy st={st} status={status} starting={starting} onGo={go} />
          <p className="plant-now mono" id="pl-now" style={{ visibility: playing && p.now ? "visible" : "hidden" }}>
            <span className="eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span id="pl-now-text">{p.now || ""}</span>
          </p>
          <div className="plant-count" id="pl-count">
            {playing && ast && (ast.done || ast.total)
              ? <><b>{ast.done ?? 0}</b><span>{ast.total ? `/ ${ast.total}` : ""}</span><small>{ast.label || ""}</small></> : null}
          </div>
          <div className="plant-action" id="pl-action">
            <Action st={st} status={status} starting={starting} onGo={go} />
          </div>
          {/* a new list whenever the notes change, so they all write themselves in again */}
          <ul className="plant-facts" id="pl-facts" key={JSON.stringify(p.facts.map((f) => f.text))}>
            {facts.map((f, i) => (
              <li key={i} className={i === 0 ? "fresh" : ""} style={vars({ "--r": `${FACT_TILT[i % 8]}deg` })}><span className="b">{BULLETS[i % BULLETS.length]}</span>{f.text}</li>
            ))}
          </ul>
        </div>
        <div className="deck" aria-hidden="true">
          <Turntable playing={playing} resting={status === "idle" || status === "paused" || status === "error"} frac={frac} />
          <div className="deck-character" id="pl-char"><Character name={want} /></div>
        </div>
        <aside className="sleeve-back" aria-label="Analysis stages">
          <p className="mono sb-head">Tracklist <span id="pl-pct">{status === "idle" ? "" : `${Math.round(frac * 100)}%`}</span></p>
          <ol className="sb-list" id="pl-list"><Tracklist st={st} status={status} /></ol>
          <p className="sb-foot mono">Pressed from your Spotify · ReccoBeats · Deezer · iTunes · MusicBrainz</p>
        </aside>
      </div>
      <div className="crate-wrap">
        <p className="mono crate-label" id="pl-crate-label">{seenCount ? `the crate · ${seenCount} sleeves so far` : "the crate · empty until your songs turn up"}</p>
        <div className="plant-crate" id="pl-crate">
          {crate.filter((c) => !broken.has(c.url)).map((c) => (
            <figure key={c.url} className="crate-cover" style={vars({ "--r": c.r })}>
              <img src={img(c.url)} alt={`${c.title} by ${c.artist}`}
                   onError={() => setBroken((b) => new Set(b).add(c.url))} />
              {c.tag ? <span className="sticker known">{c.tag}</span> : null}
              <figcaption className="mono">{c.title}<br /><span>{c.artist}</span></figcaption>
            </figure>
          ))}
        </div>
      </div>
    </section>
  );
}

interface CopyProps { st: PipelineStatus; status: PlantStatus; starting: boolean; onGo: () => void }

/** The stage of the run that's playing (or the last one, between stages). */
function useActive(st: PipelineStatus): Stage | null {
  const last = useRef<Stage | null>(null);
  const active = st.stages.find((s) => s.key === st.progress.stage) || null;
  if (active) last.current = active;
  return active ?? last.current;
}

function Copy({ st, status }: CopyProps) {
  const active = useActive(st);
  const swapped = useRef(false);
  const p = st.progress;
  let kicker = "", lines: [string, string] | null = null, blurb = "", key: string = status;

  if (status === "idle") {
    kicker = "nothing on the turntable yet";
    lines = ["Your records", "aren't pressed yet."];
    blurb = "Drop the needle and Frequency will pull your Spotify listening, listen to every song it can find, and dig for what to play next.";
  } else if (status === "paused" || status === "error") {
    kicker = active ? `stopped at ${active.side} · ${active.title.toLowerCase()}` : "the needle lifted";
    lines = ["The needle", "lifted."];
    blurb = p.error ? "" : "The run stopped before the end, probably because the program was closed. Everything measured so far is saved.";
  } else if (status === "done") {
    kicker = "side b · run out groove";
    lines = ["Pressed.", "Here's your record."];
  } else if (active) {
    kicker = `side ${active.side[0].toLowerCase()} · track ${active.side[1]} of ${st.stages.filter((s) => s.side[0] === active.side[0]).length}`;
    lines = active.lines;
    blurb = active.blurb;
    key = `stage-${active.key}`;
    swapped.current = true;               // each new stage's title slams in
  }

  return (
    <>
      <p className="mono plant-kicker" id="pl-kicker">{kicker}</p>
      <h1 key={key} className={`plant-title${swapped.current ? " swap" : ""}`} id="pl-title">
        {lines && <><span className="l1">{lines[0]}</span><span className="l2">{lines[1]}</span></>}
      </h1>
      <p className="plant-blurb" id="pl-blurb">{blurb}</p>
    </>
  );
}

function Action({ st, status, starting, onGo }: CopyProps) {
  const active = useActive(st);
  const p = st.progress;
  const ticket = (label: string, hint: string) => (
    <button className="ticket" id="pl-go" type="button" disabled={starting} onClick={onGo}>{`${label} `}<span>{starting ? "…" : hint}</span></button>
  );
  if (status === "idle") return (
    <>
      {ticket("Drop the needle", "↘ side a")}
      <ul className="plant-terms hand"><li>takes about 10–15 minutes the first time</li><li>you'll sign in to Spotify once</li><li>you can close this tab, just keep the program running and the lid open</li></ul>
    </>
  );
  if (status === "paused" || status === "error") return (
    <>{p.error ? <p className="plant-error hand">{p.error}</p> : null}{ticket("Put the needle back", "↻ resume")}</>
  );
  if (status === "running" && active?.key === "login")
    return <p className="plant-login hand">→ A Spotify tab just opened. Click <b>Agree</b>, then come back here.</p>;
  return null;
}

function Tracklist({ st, status }: { st: PipelineStatus; status: PlantStatus }) {
  const p = st.progress;
  return (
    <>
      {st.stages.map((s) => {
        const x = p.stages[s.key] || { state: "pending", done: 0, total: null };
        const state = status === "done" ? "done" : x.state;
        const f = state === "done" ? 1 : state === "active" && x.total ? clamp(x.done / x.total) : 0;
        const side = s.side === "A1" ? "side a" : s.side === "B1" ? "side b" : null;
        return [
          side && <li key={`side-${s.key}`} className="sb-side mono">{side}</li>,
          <li key={s.key} className={`sb-track ${state}`} style={vars({ "--f": f })}>
            <span className="mono n">{s.side}</span><span className="t">{s.title}</span>
            <span className="mono c">{state === "done" ? "✓" : state === "active" ? (x.total ? `${x.done}/${x.total}` : "•••") : ""}</span><span className="groove"></span>
          </li>,
        ];
      })}
    </>
  );
}

/* ---------------------------------------------------------------- turntable */
// One drawing with shared coordinates, so the record is always a perfect circle that fits the player
// box, and the needle tip is computed to sit on the grooves. Only the label layer rotates (the grooves
// look identical at every angle, like a real record), so the spin stays smooth.
const DECK = { w: 560, h: 500, cx: 236, cy: 250, platter: 214, record: 204, label: 66 };
const PIVOT = { x: 494, y: 76 };
const ARM = 252;                                   // pivot -> needle tip
const GROOVE_OUT = 192, GROOVE_IN = 82, REST = 236; // needle radius from the record's centre

function needleAngle(r: number) {
  // the tip sits where the circle of radius r (around the record) meets the arm's reach (around the pivot)
  const dx = DECK.cx - PIVOT.x, dy = DECK.cy - PIVOT.y, d = Math.hypot(dx, dy);
  const toCentre = Math.atan2(dy, dx);
  const spread = Math.acos(clamp((ARM * ARM + d * d - r * r) / (2 * ARM * d), -1, 1));
  return ((toCentre - spread) * 180) / Math.PI;
}

const GROOVES = Array.from({ length: 34 }, (_, i) => DECK.label + 10 + i * ((DECK.record - DECK.label - 14) / 33));

function Turntable({ playing, resting, frac }: { playing: boolean; resting: boolean; frac: number }) {
  const r = resting ? REST : GROOVE_OUT - clamp(frac) * (GROOVE_OUT - GROOVE_IN);
  const { cx, cy } = DECK;
  return (
    <svg className="deck-svg" viewBox={`0 0 ${DECK.w} ${DECK.h}`} role="presentation">
      <defs>
        <linearGradient id="plinth-wood" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2a2622" /><stop offset="0.6" stopColor="#1a1815" /><stop offset="1" stopColor="#1c1a17" />
        </linearGradient>
        <radialGradient id="vinyl-sheen" cx="0.35" cy="0.3" r="0.8">
          <stop offset="0" stopColor="#fff" stopOpacity="0.07" /><stop offset="0.5" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect className="deck-body" x="1" y="1" width={DECK.w - 2} height={DECK.h - 2} rx="14" fill="url(#plinth-wood)" />
      <text className="deck-brand" x={DECK.w - 22} y={DECK.h - 18}>FREQUENCY · MODEL 33</text>

      <circle cx={cx} cy={cy} r={DECK.platter + 6} className="deck-platter-rim" />
      <circle cx={cx} cy={cy} r={DECK.platter} className="deck-platter" />
      <g className="vinyl">
        <circle cx={cx} cy={cy} r={DECK.record} className="deck-record" />
        {GROOVES.map((gr, i) => <circle key={i} cx={cx} cy={cy} r={gr} className="deck-groove" />)}
        <g className={`deck-spin${playing ? " spinning" : ""}`} style={{ transformOrigin: `${cx}px ${cy}px` }}>
          {/* faint marks in the grooves so the turning is visible */}
          <circle cx={cx} cy={cy} r={150} className="deck-mark" strokeDasharray="22 90 6 140" />
          <circle cx={cx} cy={cy} r={112} className="deck-mark" strokeDasharray="40 200" />
          <circle cx={cx} cy={cy} r={DECK.label} className="deck-label" />
          <text x={cx} y={cy - 28} className="deck-label-small">SIDE A</text>
          <text x={cx} y={cy + 28} className="deck-label-big">FREQUENCY</text>
          <text x={cx} y={cy + 46} className="deck-label-small">33⅓</text>
          <circle cx={cx} cy={cy} r={4.5} className="deck-spindle" />
        </g>
        <circle cx={cx} cy={cy} r={DECK.record} fill="url(#vinyl-sheen)" pointerEvents="none" />
      </g>

      <circle cx={PIVOT.x} cy={PIVOT.y} r={30} className="arm-base" />
      <g className="deck-arm" style={{ transform: `translate(${PIVOT.x}px, ${PIVOT.y}px) rotate(${needleAngle(r)}deg)` }}>
        <circle cx={-34} cy={0} r={15} className="arm-weight" />
        <path d={`M-20 0 L${ARM - 34} 0`} className="arm-tube" />
        <path d={`M${ARM - 40} -9 L${ARM + 4} -9 L${ARM + 4} 9 L${ARM - 40} 9 Z`} className="arm-head" />
        <circle cx={ARM} cy={0} r={2.6} className="arm-needle" />
      </g>
      <circle cx={PIVOT.x} cy={PIVOT.y} r={10} className="arm-pivot" />

      <circle cx={DECK.w - 58} cy={DECK.h - 70} r={24} className="deck-knob" />
      <line x1={DECK.w - 58} y1={DECK.h - 90} x2={DECK.w - 58} y2={DECK.h - 78} className="deck-knob-mark" />
      <circle cx={DECK.w - 120} cy={DECK.h - 66} r={4.5} className={`deck-led${playing ? " on" : ""}`} />
      <text x={DECK.w - 132} y={DECK.h - 46} className="deck-speed">33 · 45</text>
    </svg>
  );
}
