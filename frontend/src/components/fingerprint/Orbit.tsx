// "Artist orbit": closer and bigger = more of the user's listening.
import { useReveal } from "../../hooks/useReveal";
import { img } from "../../lib/util";
import type { ArtistShare } from "../../types/api";
import { useTip } from "../common/Tooltip";

const C = 250, GOLD = 2.39996;

export function Orbit({ artists: A }: { artists: ArtistShare[] }) {
  const [figRef, figIn] = useReveal<HTMLElement>();
  const tip = useTip();
  const smax = Math.max(...A.map((a) => a.share));
  return (
    <figure className={`fp-orbit reveal${figIn}`} id="fp-orbit" ref={figRef}>
      <figcaption><h3>Artist <em>orbit</em></h3><p>Closer and bigger = more of your listening.</p></figcaption>
      <svg className="orbit" viewBox="-30 -30 560 560" role="img" aria-label={`Your top artists: ${A.map((a) => `${a.name} ${Math.round(a.share * 100)}%`).join(", ")}`}>
        {[100, 160, 220].map((r) => <circle key={r} className="track-ring" cx="250" cy="250" r={r} />)}
        <g className="spinner">
          {A.map((a, i) => {
            const rr = 92 + Math.pow(1 - a.share / smax, 0.7) * 128, th = i * GOLD - 1.2, x = C + Math.cos(th) * rr, y = C + Math.sin(th) * rr;
            const s = 14 + 30 * Math.sqrt(a.share / smax);
            return (
              <g key={a.name} className="planet" tabIndex={0} {...tip(<><b>{a.name}</b>{Math.round(a.share * 100)}% of your listening</>)}>
                <g className="counter" style={{ transformOrigin: `${x}px ${y}px` }}>
                  <clipPath id={`cp${i}`}><circle cx={x} cy={y} r={s} /></clipPath>
                  {a.photo
                    ? <image href={img(a.photo)} x={x - s} y={y - s} width={s * 2} height={s * 2} clipPath={`url(#cp${i})`} preserveAspectRatio="xMidYMid slice" />
                    : <circle cx={x} cy={y} r={s} fill="var(--ink-3)" />}
                  <circle className="face-ring" cx={x} cy={y} r={s + 3} />
                  {i < 4 && <text className="name" x={x} y={y + s + 22} textAnchor="middle">{a.name}</text>}
                </g>
              </g>
            );
          })}
        </g>
        <circle cx="250" cy="250" r="44" fill="var(--paper)" /><text className="you" x="250" y="259" textAnchor="middle">you</text>
      </svg>
    </figure>
  );
}
