// "The print": concentric rings that bend toward the traits the user's music leans on.
import { useMemo } from "react";
import { useReveal } from "../../hooks/useReveal";
import { WORDS, typical, type WordKey } from "../../lib/themes";
import { clamp, rand } from "../../lib/util";
import type { Profile } from "../../types/api";
import { useTip } from "../common/Tooltip";

export const FP_TRAITS: [WordKey, string, number, number][] = [
  ["energy", "energy", 0, 1], ["danceability", "groove", 0, 1], ["valence", "mood", 0, 1], ["acousticness", "acoustic", 0, 0.6],
  ["speechiness", "vocals", 0, 0.35], ["low_end_share", "bass", 0.4, 1], ["tempo_feel", "tempo", 75, 150], ["loudness", "loudness", -14, -2],
];
const N = FP_TRAITS.length, C = 250, RINGS = 18;

export function Print({ traits: T }: { traits: Profile["traits"] }) {
  const [figRef, figIn] = useReveal<HTMLElement>();
  const [svgRef, svgIn] = useReveal<SVGSVGElement>();
  const tip = useTip();

  const { rings, outer } = useMemo(() => {
    const raw = FP_TRAITS.map(([k, , lo, hi]) => clamp((typical(k, T[k]) - lo) / (hi - lo)));
    const avg = raw.reduce((a, b) => a + b, 0) / raw.length;
    const vals = raw.map((v) => clamp(0.5 + (v - avg) * 2.4));
    const f = (th: number) => {
      const u = ((((th + Math.PI / 2) / (2 * Math.PI)) % 1) + 1) % 1 * N, i = Math.floor(u) % N, t = u - Math.floor(u);
      const s = (1 - Math.cos(t * Math.PI)) / 2;
      return vals[i] * (1 - s) + vals[(i + 1) % N] * s;
    };
    const R = rand(7);
    const rings: string[] = [];
    for (let k = 0; k < RINGS; k++) {
      const base = 24 + k * 9, amp = Math.pow(k / RINGS, 1.2) * 54, gap = 0.18 + R() * 0.35, a0 = R() * Math.PI * 2;
      let d = "";
      for (let th = a0; th <= a0 + Math.PI * 2 - gap; th += 0.035) {
        const r = base + amp * (f(th) - 0.45) + Math.sin(th * 3 + k) * 1.4;
        d += `${d ? "L" : "M"}${(C + Math.cos(th) * r).toFixed(1)} ${(C + Math.sin(th) * r).toFixed(1)}`;
      }
      rings.push(d);
    }
    const outer = (th: number) => 24 + (RINGS - 1) * 9 + 54 * Math.pow((RINGS - 1) / RINGS, 1.2) * (f(th) - 0.45);
    return { rings, outer };
  }, [T]);

  const summary = FP_TRAITS.map(([k, n]) => `${n} ${WORDS[k](typical(k, T[k]))}`).join(", ");

  return (
    <figure className={`fp-print reveal${figIn}`} id="fp-print" ref={figRef}>
      <figcaption><h3>The <em>print</em></h3><p>Each ring bends toward what your music leans on.</p></figcaption>
      <svg className={`print-svg draw${svgIn}`} viewBox="-60 -40 620 580" role="img" aria-label={`Sound fingerprint: ${summary}`} ref={svgRef}>
        <g className="rings">
          {rings.map((d, k) => <path key={k} className={`ring${k === RINGS - 4 ? " hi" : ""}`} d={d} style={{ transitionDelay: `${k * 0.06}s` }} />)}
        </g>
        {FP_TRAITS.map(([k, name], i) => {
          const th = -Math.PI / 2 + (i / N) * Math.PI * 2, ro = outer(th) + 4;
          const x0 = C + Math.cos(th) * ro, y0 = C + Math.sin(th) * ro, x1 = C + Math.cos(th) * 228, y1 = C + Math.sin(th) * 228;
          const anchor = Math.abs(Math.cos(th)) < 0.3 ? "middle" : Math.cos(th) > 0 ? "start" : "end";
          const v = typical(k, T[k]), word = WORDS[k](v), dp = k === "tempo_feel" ? 0 : 2;
          const val = k === "loudness" ? v.toFixed(1) + " dB" : k === "tempo_feel" ? Math.round(v) + " BPM" : v.toFixed(2);
          return (
            <g key={k} tabIndex={0} {...tip(<><b>{name}: {word}</b>typical {val}<br />most songs {T[k].p10.toFixed(dp)} – {T[k].p90.toFixed(dp)}</>)}>
              <line className="lead" x1={x0} y1={y0} x2={x1} y2={y1} /><circle className="dot" cx={x0} cy={y0} r="4" />
              <text className="tlabel" x={x1} y={y1 + (y1 < C ? -22 : 8)} textAnchor={anchor}>{name}</text>
              <text className="tword" x={x1} y={y1 + (y1 < C ? -2 : 30)} textAnchor={anchor}>{word}</text>
              <circle className="hit" cx={x1} cy={y1} r="30" />
            </g>
          );
        })}
        <text x="250" y="256" textAnchor="middle" fontFamily="Instrument Serif" fontStyle="italic" fontSize="22" fill="var(--paper)">you</text>
      </svg>
    </figure>
  );
}
