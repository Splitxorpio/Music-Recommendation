// The recurring cast. Silhouettes built from thick colour strokes (--c-a sweater, --c-b trousers),
// with cream line details, all run through the shared #wobble filter for a hand-drawn edge.
// Colours come from CSS custom properties on the container, so each world can re-tint them.

const wrap = (vb, body, label) =>
  `<svg class="char" viewBox="${vb}" role="img" aria-label="${label}"><g class="wob">${body}</g></svg>`;

const phones = (cx, cy, r) => `
  <path class="ln" d="M${cx - r - 2} ${cy - 2} Q${cx} ${cy - r * 2.1} ${cx + r + 2} ${cy - 2}" stroke-width="3.4"/>
  <rect class="fill-a" x="${cx - r - 6}" y="${cy - 6}" width="9" height="15" rx="3"/>
  <rect class="fill-a" x="${cx + r - 3}" y="${cy - 6}" width="9" height="15" rx="3"/>`;

export const listener = () => wrap("0 0 200 200", `
  <ellipse cx="104" cy="186" rx="58" ry="5" fill="rgb(0 0 0 / .28)"/>
  <path class="limb-b" d="M84 150 L98 108 L106 172"/>
  <path class="limb-b" d="M100 152 L122 106 L130 174"/>
  <ellipse class="fill-ink ln" cx="110" cy="176" rx="10" ry="5"/>
  <ellipse class="fill-ink ln" cx="134" cy="178" rx="10" ry="5"/>
  <path class="limb-a" d="M90 152 L86 98" style="stroke-width:30"/>
  <path class="limb-a" d="M92 106 Q114 122 122 108" style="stroke-width:11"/>
  <g class="head">
    <circle class="fill-b" cx="88" cy="74" r="15"/>
    <path class="fill-ink" d="M73 73 Q74 56 90 57 Q103 58 103 70 Q95 64 86 66 Q78 67 73 73Z"/>
    <path class="ln" d="M93 80 q3 2 6 0" stroke-width="1.8"/>
    ${phones(88, 72, 15)}
  </g>
  <text class="note" x="114" y="60">♪</text><text class="note n2" x="124" y="70">♫</text>`,
  "A small figure hugging their knees, nodding along in headphones");

export const rooftop = () => wrap("0 0 220 200", `
  <circle class="fill-b" cx="172" cy="38" r="15" opacity=".92"/>
  <circle cx="166" cy="34" r="4" fill="rgb(0 0 0 / .12)"/>
  <text class="ln tw" x="40" y="40" style="font:14px serif;fill:var(--c-line,#f3ede1);stroke:none">+</text>
  <text class="ln tw" x="128" y="22" style="font:10px serif;fill:var(--c-line,#f3ede1);stroke:none;animation-delay:1s">+</text>
  <rect x="8" y="140" width="204" height="60" fill="rgb(255 255 255 / .05)"/>
  <path class="ln" d="M8 140 L212 140"/>
  ${[24, 46, 150, 172, 194].map((x, i) => `<rect class="fill-a tw" x="${x}" y="${154 + (i % 2) * 18}" width="8" height="10" style="animation-delay:${i * .7}s"/>`).join("")}
  <g class="breathe">
    <path class="limb-b" d="M96 140 L92 170" style="stroke-width:12"/>
    <path class="limb-b" d="M110 140 L116 172" style="stroke-width:12"/>
    <path class="limb-a" d="M102 142 L104 100" style="stroke-width:30"/>
    <path class="limb-a" d="M108 110 Q122 124 124 140" style="stroke-width:10"/>
    <circle class="fill-b" cx="104" cy="83" r="14"/>
    <path class="fill-ink" d="M90 82 Q90 68 104 68 Q118 68 118 82 Q111 76 104 77 Q96 77 90 82Z"/>
    ${phones(104, 82, 14)}
  </g>`,
  "A figure sitting on a rooftop ledge at night, looking at the moon");

export const dancer = () => wrap("0 0 200 220", `
  <ellipse cx="100" cy="206" rx="46" ry="5" fill="rgb(0 0 0 / .28)"/>
  <g class="sway">
    <path class="limb-b" d="M96 150 L86 200"/>
    <path class="limb-b" d="M104 150 L128 184 L142 192"/>
    <path class="limb-a" d="M100 154 L100 104" style="stroke-width:28"/>
    <path class="limb-a arm" d="M94 110 L72 86 L64 58" style="stroke-width:10"/>
    <path class="limb-a" d="M106 110 L130 90 L144 66" style="stroke-width:10"/>
    <circle class="fill-b" cx="100" cy="84" r="15"/>
    <circle class="fill-ink" cx="91" cy="72" r="10"/><circle class="fill-ink" cx="104" cy="69" r="9"/>
    ${phones(100, 83, 15)}
    <path class="ln" d="M54 50 l-9 -7 M58 40 l-3 -10 M152 58 l10 -6 M150 46 l5 -10" stroke-width="2"/>
  </g>
  <text class="note" x="150" y="96">✶</text><text class="note n2" x="40" y="104">♪</text>`,
  "A figure dancing with arms up");

export const walker = () => wrap("0 0 200 220", `
  <ellipse cx="102" cy="206" rx="44" ry="5" fill="rgb(0 0 0 / .28)"/>
  <path class="limb-b stride" d="M100 150 L86 200"/>
  <path class="limb-b stride s2" d="M102 150 L120 198"/>
  <path class="limb-a" d="M100 154 L102 98" style="stroke-width:30"/>
  <path class="limb-a" d="M106 108 Q114 130 108 146" style="stroke-width:10"/>
  <g class="head">
    <circle class="fill-b" cx="104" cy="80" r="15"/>
    <path class="ln" d="M87 88 Q84 60 105 59 Q124 60 121 88" stroke-width="3"/>
    ${phones(104, 79, 15)}
  </g>
  <path class="ln tw" d="M128 72 q6 6 0 13 M134 67 q11 11 0 23" stroke-width="2"/>`,
  "A hooded figure walking with headphones on");

export const dreamer = () => wrap("0 0 240 180", `
  <g class="breathe">
    <path class="fill-b" d="M28 132 Q28 106 56 110 Q64 86 92 94 Q114 72 142 90 Q172 80 178 108 Q208 108 206 132 Z" opacity=".95"/>
    <path class="limb-ink" d="M140 112 L188 108" style="stroke-width:13"/>
    <path class="limb-a" d="M80 113 L142 111" style="stroke-width:22"/>
    <path class="limb-a" d="M74 99 Q86 80 98 96" style="stroke-width:8"/>
    <circle class="fill-b ln" cx="66" cy="104" r="13"/>
    <path class="fill-ink" d="M53 102 Q55 89 68 90 Q78 92 79 100 Q70 96 62 98Z"/>
    ${phones(66, 103, 13)}
  </g>
  <rect class="fill-a" x="118" y="34" width="22" height="22" transform="rotate(-12 129 45)" style="animation:float 7s ease-in-out infinite"/>
  <rect class="fill-ink ln" x="160" y="52" width="16" height="16" transform="rotate(10 168 60)" style="animation:float 9s ease-in-out infinite 1s"/>
  <text class="note" x="92" y="72" style="font-family:var(--f-hand)">z</text><text class="note n2" x="100" y="62" style="font-family:var(--f-hand)">z</text>`,
  "A figure lying on a cloud with headphones, album covers floating above");

export const digger = () => wrap("0 0 220 200", `
  <ellipse cx="120" cy="190" rx="90" ry="5" fill="rgb(0 0 0 / .28)"/>
  <rect class="fill-a" x="118" y="96" width="42" height="42" transform="rotate(-9 139 117)"/>
  <rect class="fill-b" x="140" y="92" width="42" height="42" transform="rotate(6 161 113)"/>
  <rect class="fill-ink ln" x="160" y="100" width="40" height="40" transform="rotate(14 180 120)"/>
  <rect class="fill-ink ln" x="108" y="128" width="98" height="56"/>
  <path class="ln" d="M108 146 L206 146 M108 164 L206 164" stroke-width="1.6"/>
  <path class="limb-b" d="M70 186 L76 150"/>
  <path class="limb-b" d="M88 186 L86 150"/>
  <path class="limb-a" d="M80 154 L92 106" style="stroke-width:28"/>
  <path class="limb-a arm" d="M96 114 L128 124" style="stroke-width:10"/>
  <g class="head">
    <circle class="fill-b" cx="98" cy="86" r="14"/>
    <path class="fill-a" d="M84 83 Q95 66 111 79 L120 82 L84 85Z"/>
  </g>
  <path class="ln" d="M88 100 Q98 109 108 100" stroke-width="3"/>`,
  "A figure flipping through a crate of records");

export const CAST = { listener, rooftop, dancer, walker, dreamer, digger };
