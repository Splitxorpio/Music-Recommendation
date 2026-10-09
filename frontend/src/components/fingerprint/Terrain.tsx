// "Tempo terrain": one ridge of BPMs per world, with a crosshair that reads every ridge at the pointer.
import { Fragment, useMemo, useRef, useState, type PointerEvent } from "react";
import { useReveal } from "../../hooks/useReveal";
import { clamp } from "../../lib/util";
import type { Profile } from "../../types/api";
import { useTooltip } from "../common/Tooltip";
import { useWorldOf } from "../common/WorldContext";

const W = 640, padL = 130, padR = 16, H = 92, step = 52, top = 26;

export function Terrain({ landscapes: L, bpm: [b0, b1] }: { landscapes: Profile["landscapes"]; bpm: [number, number] }) {
  const worldOf = useWorldOf();
  const tip = useTooltip();
  const [figRef, figIn] = useReveal<HTMLElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const [cross, setCross] = useState<number | null>(null);
  const height = top + (L.length - 1) * step + H + 40;

  const ridges = useMemo(() => {
    const x = (i: number, n: number) => padL + (i / (n - 1)) * (W - padL - padR);
    return L.map((l, j) => {
      const base = top + j * step + H, w = worldOf(l.cluster), n = l.points.length, stroke = w ? w.mark : "var(--paper)";
      let d = `M${padL} ${base}`;
      l.points.forEach((p, i) => (d += ` L${x(i, n).toFixed(1)} ${(base - p * H).toFixed(1)}`));
      d += ` L${W - padR} ${base}`;
      return (
        <Fragment key={l.cluster}>
          <path d={`${d} Z`} fill="var(--ink)" /><path className="ridge" d={d} fill="none" stroke={stroke} />
          <line x1={padL - 10} y1={base} x2={padL - 2} y2={base} stroke={stroke} strokeWidth="2" />
          <text className="rlabel" x={padL - 16} y={base + 5} textAnchor="end">{w ? w.name : `World ${j + 1}`}</text>
        </Fragment>
      );
    });
  }, [L, worldOf]);

  const move = (e: PointerEvent) => {
    const r = svgRef.current!.getBoundingClientRect(), vx = ((e.clientX - r.left) / r.width) * W;
    const frac = clamp((vx - padL) / (W - padL - padR)), bpm = Math.round(b0 + frac * (b1 - b0));
    setCross(vx);
    const rows = L.map((l) => ({ name: worldOf(l.cluster)?.name ?? "", v: l.points[Math.round(frac * (l.points.length - 1))] })).sort((a, b) => b.v - a.v);
    tip.show(<><b>{bpm} BPM</b>{rows.map((rw, i) => <span key={rw.name + i}>{i > 0 && <br />}{rw.name} {"▮".repeat(Math.round(rw.v * 8)) || "·"}</span>)}</>, e.clientX, e.clientY);
  };

  return (
    <figure className={`fp-terrain reveal${figIn}`} id="fp-terrain" ref={figRef}>
      <figcaption><h3>Tempo <em>terrain</em></h3><p>Where your BPMs pile up — one ridge per world.</p></figcaption>
      <svg className="terrain" viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Tempo distribution per world" ref={svgRef}>
        {ridges}
        <g className="axis">
          <line x1={padL} y1={height - 30} x2={W - padR} y2={height - 30} />
          {[60, 80, 100, 120, 140, 160, 180, 200].map((b) => {
            const tx = padL + ((b - b0) / (b1 - b0)) * (W - padL - padR);
            return <Fragment key={b}><line x1={tx} y1={height - 30} x2={tx} y2={height - 24} /><text x={tx} y={height - 10} textAnchor="middle">{b}</text></Fragment>;
          })}
          <text x={padL - 16} y={height - 10} textAnchor="end">BPM →</text>
        </g>
        <line className="cross" x1={cross ?? 0} y1={top - 10} x2={cross ?? 0} y2={height - 30} style={cross == null ? undefined : { opacity: 0.6 }} />
        <rect className="hover-pad" x={padL} y="0" width={W - padL - padR} height={height - 30}
          onPointerMove={move} onPointerLeave={() => { setCross(null); tip.hide(); }} />
      </svg>
    </figure>
  );
}
