// "Genre constellation": top genres as stars, joined when the same library artists carry both.
import { useMemo } from "react";
import { useReveal } from "../../hooks/useReveal";
import { clamp, rand } from "../../lib/util";
import type { Profile } from "../../types/api";
import { useTip } from "../common/Tooltip";

const W = 680, H = 460;

/** A tiny deterministic force layout: same input, same picture. */
export function layoutStars(genres: Profile["genres"], links: Profile["genre_links"]) {
  const G = genres.slice(0, 12), E = links.filter((e) => G.some((g) => g[0] === e.a) && G.some((g) => g[0] === e.b));
  const R = rand(3);
  const nodes = G.map(([name, share], i) => ({ name, share, x: W / 2 + Math.cos(i * 2.4) * 140 + R() * 10, y: H / 2 + Math.sin(i * 2.4) * 120 + R() * 10 }));
  const idx = Object.fromEntries(nodes.map((n, i) => [n.name, i]));
  for (let it = 0; it < 400; it++) {
    for (const a of nodes) for (const b of nodes) if (a !== b) {
      const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01, f = 3400 / d2;
      a.x += dx * f * 0.02; a.y += dy * f * 0.02;
    }
    for (const e of E) {
      const a = nodes[idx[e.a]], b = nodes[idx[e.b]], dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy), k = (d - 140) * 0.004 * (0.4 + e.w);
      a.x += dx / d * k * d * 0.1; a.y += dy / d * k * d * 0.1; b.x -= dx / d * k * d * 0.1; b.y -= dy / d * k * d * 0.1;
    }
    for (const n of nodes) { n.x += (W / 2 - n.x) * 0.01; n.y += (H / 2 - n.y) * 0.01; n.x = clamp(n.x, 40, W - 170); n.y = clamp(n.y, 30, H - 30); }
  }
  return { nodes, edges: E.map((e) => ({ a: nodes[idx[e.a]], b: nodes[idx[e.b]], w: e.w })), top: G };
}

export function Stars({ genres, links }: { genres: Profile["genres"]; links: Profile["genre_links"] }) {
  const [figRef, figIn] = useReveal<HTMLElement>();
  const tip = useTip();
  const { nodes, edges, top } = useMemo(() => layoutStars(genres, links), [genres, links]);
  const gmax = top[0]?.[1] ?? 1;
  return (
    <figure className={`fp-stars reveal${figIn}`} id="fp-stars" ref={figRef}>
      <figcaption><h3>Genre <em>constellation</em></h3><p>Lines join genres your artists share.</p></figcaption>
      <svg className="stars" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Your genres: ${top.map((g) => `${g[0]} ${Math.round(g[1] * 100)}%`).join(", ")}`}>
        {edges.map((e) => (
          <line key={`${e.a.name}|${e.b.name}`} className="edge" x1={e.a.x} y1={e.a.y} x2={e.b.x} y2={e.b.y}
            strokeOpacity={(0.1 + e.w * 0.5).toFixed(2)} strokeWidth={(0.8 + e.w * 1.4).toFixed(2)} />
        ))}
        {nodes.map((n, i) => {
          const r = 3 + 11 * Math.sqrt(n.share / gmax);
          return (
            <g key={n.name} className="s" tabIndex={0} {...tip(<><b>{n.name}</b>{Math.round(n.share * 100)}% of your genre mix</>)}>
              <circle className="halo" cx={n.x} cy={n.y} r={r * 2.6} style={{ animationDelay: `${(i * 0.37) % 4}s` }} /><circle className="star" cx={n.x} cy={n.y} r={r} />
              <text className="slabel" x={n.x + r + 7} y={n.y + 5}>{n.name}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
