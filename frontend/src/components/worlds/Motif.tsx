// Generated backdrop for each world (night city, loud rays, 808 rings, soft clouds). Seeded per world,
// so it's the same drawing on every visit.
import { useMemo, type ReactElement } from "react";
import type { ThemeKey } from "../../lib/themes";
import { rand } from "../../lib/util";

const SVG = { viewBox: "0 0 1200 800", preserveAspectRatio: "xMidYMid slice", width: "100%", height: "100%", "aria-hidden": true } as const;

export function Motif({ theme, id }: { theme: ThemeKey; id: number }) {
  return useMemo(() => draw(theme, id), [theme, id]);
}

function draw(key: ThemeKey, id: number): ReactElement {
  const R = rand(id * 97 + 13);
  if (key === "night") {
    const sky: ReactElement[] = [], wins: ReactElement[] = [];
    let city = "M0 800 L0 610";
    for (let i = 0; i < 70; i++) {
      const cx = R() * 1200, cy = R() * 420, r = R() * 1.6 + 0.4, dur = 3 + R() * 4, delay = R() * 4;
      sky.push(<circle key={i} cx={cx} cy={cy} r={r} fill="#e6ecff" className="tw" style={{ animation: `twinkle ${dur}s ease-in-out ${delay}s infinite` }} />);
    }
    let x = 0;
    while (x < 1200) {
      const w = 40 + R() * 70, h = 120 + R() * 230;
      city += ` L${x} ${800 - h} L${x + w} ${800 - h}`;
      for (let k = 0; k < 6; k++) if (R() > 0.45) {
        const wx = x + 8 + R() * (w - 20), wy = 800 - h + 14 + R() * (h - 40), dur = 2 + R() * 5, delay = R() * 5;
        wins.push(<rect key={wins.length} x={wx} y={wy} width="5" height="7" fill="#ffd27a" style={{ animation: `twinkle ${dur}s ease-in-out ${delay}s infinite` }} />);
      }
      x += w;
    }
    city += " L1200 800 Z";
    return (
      <svg {...SVG}>
        <defs><radialGradient id={`ng${id}`} cx="70%" cy="20%" r="80%"><stop offset="0" stopColor="#1f3a8a" stopOpacity=".55" /><stop offset="1" stopColor="#0b1230" stopOpacity="0" /></radialGradient></defs>
        <rect width="1200" height="800" fill={`url(#ng${id})`} />{sky}<path d={city} fill="#060a1d" />{wins}
      </svg>
    );
  }
  if (key === "volume") {
    const rays = Array.from({ length: 24 }, (_, i) => {
      const a0 = (i / 24) * Math.PI * 2, a1 = a0 + Math.PI / 24;
      return <path key={i} d={`M900 300 L${900 + Math.cos(a0) * 1400} ${300 + Math.sin(a0) * 1400} L${900 + Math.cos(a1) * 1400} ${300 + Math.sin(a1) * 1400} Z`} fill="#ff8a4c" opacity=".07" />;
    });
    // stickers live in the top and bottom margins so they never sit on the headline
    const tags = ["LOUD", "+ + +", "PLAY IT AGAIN", "★", "ON REPEAT"].map((t, i) => {
      const tx = 60 + R() * 980, ty = i % 2 ? 40 + R() * 50 : 720 + R() * 50, rot = R() * 30 - 15;
      return (
        <g key={t} transform={`translate(${tx} ${ty}) rotate(${rot})`}>
          <rect x="-8" y="-24" width={t.length * 17 + 16} height="34" fill={i % 2 ? "#ffeee3" : "#ff6b5a"} opacity=".9" />
          <text x="0" y="0" fontFamily="DM Mono" fontSize="20" fill="#2a130b">{t}</text>
        </g>
      );
    });
    return (
      <svg {...SVG}>{rays}
        <circle cx="900" cy="300" r="150" fill="#ff8a4c" opacity=".22" />
        <text x="-20" y="780" fontFamily="Bricolage Grotesque" fontWeight="800" fontSize="300" fill="none" stroke="#ff8a4c" strokeOpacity=".18" strokeWidth="2" letterSpacing="-18" transform="skewX(-8)" filter="url(#wobble-strong)">TURN IT UP</text>
        <g opacity=".55">{tags}</g>
      </svg>
    );
  }
  if (key === "lowend") {
    const rings = Array.from({ length: 13 }, (_, j) => {
      const i = j + 1;
      return <circle key={i} cx="960" cy="420" r={i * 42} fill="none" stroke={i % 4 ? "#11917f" : "#c6f432"} strokeOpacity={0.5 - i * 0.03} strokeWidth={i % 4 ? 1.5 : 3} />;
    });
    return (
      <svg {...SVG}>
        <defs>
          <pattern id={`ht${id}`} width="12" height="12" patternUnits="userSpaceOnUse"><circle cx="6" cy="6" r="1.8" fill="#c6f432" opacity=".18" /></pattern>
          <radialGradient id={`hm${id}`} cx="20%" cy="80%" r="60%"><stop offset="0" stopColor="#fff" /><stop offset="1" stopColor="#fff" stopOpacity="0" /></radialGradient>
          <mask id={`hk${id}`}><rect width="1200" height="800" fill={`url(#hm${id})`} /></mask>
        </defs>
        <rect width="1200" height="800" fill={`url(#ht${id})`} mask={`url(#hk${id})`} />
        <g style={{ transformOrigin: "960px 420px", animation: "breathe 1.1s ease-in-out infinite" }}>{rings}</g>
        <text x="40" y="250" fontFamily="Bricolage Grotesque" fontWeight="800" fontSize="280" fill="none" stroke="#c6f432" strokeOpacity=".12" strokeWidth="2" letterSpacing="-14">808</text>
      </svg>
    );
  }
  const blobs = Array.from({ length: 9 }, (_, i) => {
    const cx = R() * 1200, cy = R() * 800, rx = 80 + R() * 160, ry = 50 + R() * 90, op = 0.35 + R() * 0.35, dur = 10 + R() * 8, delay = R() * 5;
    return <ellipse key={i} cx={cx} cy={cy} rx={rx} ry={ry} fill={i % 2 ? "#ffffff" : "#d6c2f0"} opacity={op} style={{ animation: `float ${dur}s ease-in-out ${delay}s infinite` }} />;
  });
  return (
    <svg {...SVG}>
      <defs><filter id={`bl${id}`}><feGaussianBlur stdDeviation="30" /></filter></defs><g filter={`url(#bl${id})`}>{blobs}</g>
      <text x="620" y="760" fontFamily="Instrument Serif" fontStyle="italic" fontSize="240" fill="#8a52a8" opacity=".1">hush</text>
    </svg>
  );
}
