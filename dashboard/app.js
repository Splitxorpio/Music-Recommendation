import { CAST } from "./characters.js";
import { $, $$, esc, img, pad, clamp, REDUCED, rand, because, sampleColour } from "./util.js";
import { startPlant } from "./plant.js";

/* ---------------------------------------------------------------- world themes */
// Each sub-taste gets a visual universe, chosen from the words in its auto-label.
const THEMES = {
  night:  { words: { dark: 3, slow: 2, quieter: 1, "laid-back": 1, "less danceable": 1 }, lines: ["After", "dark"], kicker: "the 2 a.m. side", poster: "#8fb0ff", mark: "#4a78ff", char: "rooftop", tint: { a: "#8fb0ff", b: "#e6ecff", ink: "#05091c", line: "#e6ecff" } },
  volume: { words: { upbeat: 3, "high-energy": 3, loud: 2, fast: 2, danceable: 2 }, lines: ["Volume", "up"], kicker: "the loud side", poster: "#ff9a5c", mark: "#e66a2a", char: "dancer", tint: { a: "#ff8a4c", b: "#ffeee3", ink: "#1a0b05", line: "#ffeee3" } },
  lowend: { words: { "bass-heavy": 3, "rap-heavy": 3, quieter: 1, slow: 1 }, lines: ["Low end", "theory"], kicker: "the heavy side", poster: "#c6f432", mark: "#11917f", char: "walker", tint: { a: "#c6f432", b: "#e3f5ee", ink: "#051210", line: "#e3f5ee" } },
  soft:   { words: { acoustic: 3, "light on bass": 2, melodic: 2, "laid-back": 2, vocal: 1, quieter: 1 }, lines: ["Soft", "focus"], kicker: "the tender side", poster: "#d9b8ff", mark: "#c070c8", char: "dreamer", tint: { a: "#8a52a8", b: "#ffffff", ink: "#241a33", line: "#241a33" } },
};

function assignThemes(clusters) {
  const pairs = [];
  for (const c of clusters) {
    const parts = c.label.split(",").map((s) => s.trim());
    for (const [key, t] of Object.entries(THEMES)) {
      const score = parts.reduce((acc, p, i) => acc + (t.words[p] || 0) * (3 - i), 0);
      pairs.push({ id: c.id, key, score });
    }
  }
  pairs.sort((a, b) => b.score - a.score);
  const out = {}, used = new Set();
  for (const p of pairs) if (!(p.id in out) && !used.has(p.key)) { out[p.id] = p.key; used.add(p.key); }
  return out;
}

/* ---------------------------------------------------------------- plain words for traits */
const WORDS = {
  energy: (v) => (v < 0.5 ? "calm" : v < 0.63 ? "steady" : v < 0.74 ? "charged" : "full-tilt"),
  valence: (v) => (v < 0.3 ? "dark" : v < 0.45 ? "moody" : v < 0.6 ? "bright-ish" : "sunny"),
  low_end_share: (v) => (v < 0.65 ? "light" : v < 0.78 ? "solid" : "heavy"),
  speechiness: (v) => (v < 0.08 ? "sung" : v < 0.16 ? "half-rapped" : "rapped"),
  danceability: (v) => (v < 0.55 ? "loose" : v < 0.7 ? "nodding" : "dancey"),
  acousticness: (v) => (v < 0.1 ? "all machine" : v < 0.3 ? "mostly produced" : "organic"),
  tempo_feel: (v) => `~${Math.round(v)} bpm`,
  loudness: (v) => (v < -9 ? "hushed" : v < -6.5 ? "present" : "loud"),
};

/* ---------------------------------------------------------------- tooltip */
const tip = $("#tooltip");
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.classList.add("on");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${clamp(x + 14, 8, innerWidth - w - 8)}px`;
  tip.style.top = `${clamp(y - h - 12, 8, innerHeight - h - 8)}px`;
}
const hideTip = () => tip.classList.remove("on");
document.addEventListener("pointermove", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (t) showTip(t.dataset.tip, e.clientX, e.clientY);
  else if (!e.target.closest?.(".hover-pad")) hideTip();
});
document.addEventListener("focusin", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (t) { const r = t.getBoundingClientRect(); showTip(t.dataset.tip, r.left + r.width / 2, r.top); }
});
document.addEventListener("focusout", hideTip);

/* ---------------------------------------------------------------- artwork colour */
function tintFromArt(el, url, prop = "--c") {
  sampleColour(url).then((rgb) => rgb && el.style.setProperty(prop, rgb));
}

/* ---------------------------------------------------------------- observers & motion */
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
}, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
const watch = (root = document) => $$(".reveal:not(.in), .stagger:not(.in), .draw:not(.in)", root).forEach((el) => io.observe(el));

function tilt(root = document) {
  if (REDUCED) return;
  $$("[data-tilt]", root).forEach((el) => {
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
      el.style.transform = `perspective(900px) rotateY(${x * 9}deg) rotateX(${y * -7}deg)`;
    });
    el.addEventListener("pointerleave", () => (el.style.transform = ""));
  });
}

addEventListener("scroll", () => $(".masthead").classList.toggle("scrolled", scrollY > 40), { passive: true });
const navIO = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) $$(".masthead nav a").forEach((a) => a.classList.toggle("active", a.hash === `#${e.target.id}`));
}, { rootMargin: "-45% 0px -50% 0px" });
["crate", "desk", "worlds", "fingerprint", "notes"].forEach((id) => navIO.observe(document.getElementById(id)));

/* ---------------------------------------------------------------- state */
let PROFILE, THEME_OF = {};
const DEFAULTS = { ws: 50, wc: 25, wn: 25, known: false, far: false, bpm: null, bass: null };
const state = { ...DEFAULTS };

const worldOf = (cid) => {
  const key = THEME_OF[cid];
  return key ? { key, ...THEMES[key], name: `${THEMES[key].lines[0]} ${THEMES[key].lines[1]}` } : null;
};
const stickers = (r) =>
  (r.mode === "known" ? `<span class="sticker known" style="--r:2deg">familiar</span>` : "") +
  (r.hop === 2 ? `<span class="sticker far" style="--r:-3deg">2 steps out</span>` : "") +
  (r.estimated ? `<span class="sticker est" style="--r:3deg" data-tip="Measured from a 30-second preview, then calibrated">≈ estimated</span>` : "");
const glyph = (p) => {
  const ring = (r, v, op) => { const c = 2 * Math.PI * r; return `<circle class="bg" cx="17" cy="17" r="${r}"/><circle class="v" cx="17" cy="17" r="${r}" stroke-dasharray="${(c * (v ?? 0)) / 100} ${c}" opacity="${op}"/>`; };
  const t = `<b>Why it scored</b>Sound ${Math.round(p.sound ?? 0)} · Scene ${p.scene == null ? "?" : Math.round(p.scene)} · Connection ${Math.round(p.connection ?? 0)}<br><small>outer → inner</small>`;
  return `<svg class="glyph" viewBox="0 0 34 34" data-tip="${esc(t)}" aria-label="${esc(t.replace(/<[^>]+>/g, " "))}">${ring(15, p.sound, 1)}${ring(10.5, p.scene, 0.75)}${ring(6, p.connection, 0.5)}</svg>`;
};

/* ---------------------------------------------------------------- hero */
function renderHero(items) {
  const el = $("#hero-inner");
  const r = items[0];
  if (!r) {
    el.innerHTML = `<div class="empty" style="grid-column:1/-1"><div>${CAST.digger()}</div><p>Nothing in the crate at these settings. <em>Open the dials up a little.</em></p></div>`;
    return;
  }
  const w = worldOf(r.cluster);
  const arrow = `<svg viewBox="0 0 46 34" aria-hidden="true"><path d="M4 4 C 6 20, 18 28, 40 26 M30 18 L41 26 L31 32" fill="none" stroke="var(--acid)" stroke-width="2.2" stroke-linecap="round" filter="url(#wobble)"/></svg>`;
  el.innerHTML = `
    <div class="hero-kicker">
      <span class="mono">Issue ${esc(PROFILE.meta.date.split("-").reverse().join("."))} — Pick Nº 01 of ${items.length}</span>
      <span class="rule"></span>
      ${w ? `<span class="sticker" style="--r:-3deg;background:${w.tint.a};color:${w.tint.ink}">${esc(w.name)}</span>` : ""}
      ${stickers(r)}
    </div>
    <h1 class="hero-type"><span class="l1">Your next</span><span class="l2">obsession.</span></h1>
    <div class="hero-art">
      <div class="vinyl" aria-hidden="true"></div>
      <a class="sleeve" href="${esc(r.spotify_url || "#")}" target="_blank" rel="noopener" data-tilt>
        <img src="${img(r.cover)}" alt="Cover of ${esc(r.album || r.title)} by ${esc(r.artist)}">
      </a>
    </div>
    <div class="stamp" tabindex="0" aria-label="Match ${Math.round(r.score)} out of 100">
      <svg class="ring" viewBox="0 0 120 120" aria-hidden="true"><defs><path id="circ" d="M60,60 m-50,0 a50,50 0 1,1 100,0 a50,50 0 1,1 -100,0"/></defs><text><textPath href="#circ">sound · scene · connection · your match ·</textPath></text></svg>
      <b>${Math.round(r.score)}</b><small>/ 100</small>
      <span class="parts">Sound ${Math.round(r.parts.sound ?? 0)} · Scene ${r.parts.scene == null ? "?" : Math.round(r.parts.scene) + (r.scene_inferred ? "~" : "")} · Connection ${Math.round(r.parts.connection)}</span>
    </div>
    <div class="hero-character" style="--c-a:var(--acid);--c-b:var(--cream)">${CAST.listener()}</div>
    <div class="hero-lines">
      <p class="title">${esc(r.title)}</p>
      <p class="artist">${esc(r.artists.join(", ") || r.artist)}</p>
      <p class="because hand">${arrow}<span>${esc(because(r))}</span></p>
      <p class="meta mono">${r.tempo ? `<span>${r.tempo} BPM</span>` : ""}${r.bass_word ? `<span>${r.bass_word} bass</span>` : ""}${r.genres.length ? `<span>${esc(r.genres.slice(0, 2).join(" / "))}</span>` : ""}${r.album ? `<span>from “${esc(r.album)}”</span>` : ""}</p>
      ${r.spotify_url ? `<a class="ticket" href="${esc(r.spotify_url)}" target="_blank" rel="noopener">Play on Spotify <span>↗ admit one</span></a>` : ""}
    </div>
    <div class="barcode mono"><span>Vol. ${PROFILE.meta.songs} songs</span><i aria-hidden="true"></i></div>`;
  tintFromArt(document.documentElement, r.cover, "--amb");
  tilt(el);
}

/* ---------------------------------------------------------------- crate */
function renderCrate(items) {
  // Pick 1 is the hero above; picks 2-25 sit in one fixed grid: every cover has its own spot,
  // nothing overlaps or scrolls sideways, and hovering never moves a neighbour.
  const body = $("#crate-body");
  const rest = items.slice(1);
  $("#crate-count").textContent = items.length ? `${items.length} records pulled for you · scored on sound, scene & connection` : "empty at these settings";
  if (!items.length) {
    body.innerHTML = `<div class="empty"><div>${CAST.digger()}</div><p>The crate's empty. <em>Loosen a filter on the desk.</em></p></div>`;
    return;
  }
  body.innerHTML = `<ol class="crate-grid stagger">${rest.map((r, i) => {
    const w = worldOf(r.cluster);
    const tip = `<b>${esc(r.title)}</b>${esc(r.artist)}<br>${esc(because(r))}<br>Sound ${Math.round(r.parts.sound ?? 0)} · Scene ${r.parts.scene == null ? "?" : Math.round(r.parts.scene)} · Connection ${Math.round(r.parts.connection ?? 0)}`;
    return `<li><a class="pick" href="${esc(r.spotify_url || "#")}" target="_blank" rel="noopener" data-art="${esc(r.cover_m || r.cover)}" data-tip="${esc(tip)}">
      <span class="art"><img loading="lazy" src="${img(r.cover_m || r.cover)}" alt="Cover of ${esc(r.album || r.title)} by ${esc(r.artist)}">
        <span class="n mono">${pad(i + 2)}</span><span class="score mono">${Math.round(r.score)}</span></span>
      <b class="t">${esc(r.title)}</b>
      <span class="a">${esc(r.artist)}</span>
      <i class="why">${esc(because(r))}</i>
      <span class="meta mono">${w ? `<i class="swatch" style="background:${w.mark}" title="${esc(w.name)}"></i>${esc(w.name)}` : ""}${r.tempo ? ` · ${r.tempo} bpm` : ""}${r.estimated ? ` · <span class="est-mark">≈</span>` : ""}${r.hop === 2 ? " · far" : ""}${r.mode === "known" ? " · familiar" : ""}</span>
    </a></li>`;
  }).join("")}</ol>`;
  $$("[data-art]", body).forEach((el) => tintFromArt(el, el.dataset.art));
  watch(body);
}

/* ---------------------------------------------------------------- desk */
const fetchState = { timer: 0, seq: 0 };
async function loadRecs() {
  const q = new URLSearchParams({ ws: state.ws / 100, wc: state.wc / 100, wn: state.wn / 100, known: state.known ? 1 : 0, far: state.far ? 1 : 0, n: 25 });
  if (state.bpm) q.set("bpm", state.bpm.join(","));
  if (state.bass) q.set("bass", state.bass.join(","));
  const seq = ++fetchState.seq;
  $("#lamp").classList.add("busy"); $("#status-text").textContent = "re-digging…";
  const res = await fetch(`/api/recommendations?${q}`).then((r) => r.json());
  if (seq !== fetchState.seq) return;
  renderHero(res.items); renderCrate(res.items);
  $("#lamp").classList.remove("busy"); $("#status-text").textContent = `live · ${res.count} records`;
}
const queue = () => { clearTimeout(fetchState.timer); fetchState.timer = setTimeout(loadRecs, 220); };

function setupDesk() {
  const { bpm, bass } = PROFILE.ranges;
  $$(".fader input").forEach((inp) => {
    inp.value = state[inp.dataset.w];
    inp.addEventListener("input", () => { state[inp.dataset.w] = +inp.value; $(`[data-o="${inp.dataset.w}"]`).textContent = inp.value; queue(); });
  });
  $("#opt-known").addEventListener("change", (e) => { state.known = e.target.checked; queue(); });
  $("#opt-far").addEventListener("change", (e) => { state.far = e.target.checked; queue(); });

  const setupRail = (name, [min, max], step, fmt) => {
    const rail = $(`[data-rail="${name}"]`), [lo, hi] = $$("input", rail), fill = $(".fill", rail), led = $(`[data-led="${name}"]`);
    for (const i of [lo, hi]) Object.assign(i, { min, max, step });
    lo.value = min; hi.value = max;
    const paint = () => {
      let a = +lo.value, b = +hi.value;
      if (a > b) [a, b] = [b, a];
      fill.style.left = `${((a - min) / (max - min)) * 100}%`;
      fill.style.right = `${100 - ((b - min) / (max - min)) * 100}%`;
      led.textContent = `${fmt(a)} – ${fmt(b)}`;
      state[name] = a <= min && b >= max ? null : [a, b];
    };
    for (const i of [lo, hi]) i.addEventListener("input", () => { paint(); queue(); });
    paint();
    rail.reset = () => { lo.value = min; hi.value = max; paint(); };
  };
  setupRail("bpm", bpm, 1, (v) => Math.round(v));
  setupRail("bass", bass, 0.01, (v) => v.toFixed(2));

  $("#reset").addEventListener("click", () => {
    Object.assign(state, DEFAULTS);
    $$(".fader input").forEach((i) => { i.value = state[i.dataset.w]; $(`[data-o="${i.dataset.w}"]`).textContent = i.value; });
    $("#opt-known").checked = false; $("#opt-far").checked = false;
    $$(".rail").forEach((r) => r.reset());
    queue();
  });
}

/* ---------------------------------------------------------------- worlds */
function motif(key, id) {
  const R = rand(id * 97 + 13);
  if (key === "night") {
    let sky = "", city = "M0 800 L0 610";
    for (let i = 0; i < 70; i++) sky += `<circle cx="${R() * 1200}" cy="${R() * 420}" r="${R() * 1.6 + 0.4}" fill="#e6ecff" class="tw" style="animation:twinkle ${3 + R() * 4}s ease-in-out ${R() * 4}s infinite"/>`;
    let x = 0, wins = "";
    while (x < 1200) {
      const w = 40 + R() * 70, h = 120 + R() * 230;
      city += ` L${x} ${800 - h} L${x + w} ${800 - h}`;
      for (let k = 0; k < 6; k++) if (R() > 0.45) wins += `<rect x="${x + 8 + R() * (w - 20)}" y="${800 - h + 14 + R() * (h - 40)}" width="5" height="7" fill="#ffd27a" style="animation:twinkle ${2 + R() * 5}s ease-in-out ${R() * 5}s infinite"/>`;
      x += w;
    }
    city += " L1200 800 Z";
    return `<svg viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice" width="100%" height="100%" aria-hidden="true">
      <defs><radialGradient id="ng${id}" cx="70%" cy="20%" r="80%"><stop offset="0" stop-color="#1f3a8a" stop-opacity=".55"/><stop offset="1" stop-color="#0b1230" stop-opacity="0"/></radialGradient></defs>
      <rect width="1200" height="800" fill="url(#ng${id})"/>${sky}<path d="${city}" fill="#060a1d"/>${wins}</svg>`;
  }
  if (key === "volume") {
    let rays = "";
    for (let i = 0; i < 24; i++) { const a0 = (i / 24) * Math.PI * 2, a1 = a0 + Math.PI / 24; rays += `<path d="M900 300 L${900 + Math.cos(a0) * 1400} ${300 + Math.sin(a0) * 1400} L${900 + Math.cos(a1) * 1400} ${300 + Math.sin(a1) * 1400} Z" fill="#ff8a4c" opacity=".07"/>`; }
    // stickers live in the top and bottom margins so they never sit on the headline
    const tags = ["LOUD", "+ + +", "PLAY IT AGAIN", "★", "ON REPEAT"].map((t, i) => `<g transform="translate(${60 + R() * 980} ${i % 2 ? 40 + R() * 50 : 720 + R() * 50}) rotate(${R() * 30 - 15})"><rect x="-8" y="-24" width="${t.length * 17 + 16}" height="34" fill="${i % 2 ? "#ffeee3" : "#ff6b5a"}" opacity=".9"/><text x="0" y="0" font-family="DM Mono" font-size="20" fill="#2a130b">${t}</text></g>`).join("");
    return `<svg viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice" width="100%" height="100%" aria-hidden="true">${rays}
      <circle cx="900" cy="300" r="150" fill="#ff8a4c" opacity=".22"/>
      <text x="-20" y="780" font-family="Bricolage Grotesque" font-weight="800" font-size="300" fill="none" stroke="#ff8a4c" stroke-opacity=".18" stroke-width="2" letter-spacing="-18" transform="skewX(-8)" filter="url(#wobble-strong)">TURN IT UP</text>
      <g opacity=".55">${tags}</g></svg>`;
  }
  if (key === "lowend") {
    let rings = "";
    for (let i = 1; i < 14; i++) rings += `<circle cx="960" cy="420" r="${i * 42}" fill="none" stroke="${i % 4 ? "#11917f" : "#c6f432"}" stroke-opacity="${0.5 - i * 0.03}" stroke-width="${i % 4 ? 1.5 : 3}"/>`;
    return `<svg viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice" width="100%" height="100%" aria-hidden="true">
      <defs><pattern id="ht${id}" width="12" height="12" patternUnits="userSpaceOnUse"><circle cx="6" cy="6" r="1.8" fill="#c6f432" opacity=".18"/></pattern>
      <radialGradient id="hm${id}" cx="20%" cy="80%" r="60%"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient><mask id="hk${id}"><rect width="1200" height="800" fill="url(#hm${id})"/></mask></defs>
      <rect width="1200" height="800" fill="url(#ht${id})" mask="url(#hk${id})"/>
      <g style="transform-origin:960px 420px;animation:breathe 1.1s ease-in-out infinite">${rings}</g>
      <text x="40" y="250" font-family="Bricolage Grotesque" font-weight="800" font-size="280" fill="none" stroke="#c6f432" stroke-opacity=".12" stroke-width="2" letter-spacing="-14">808</text></svg>`;
  }
  let blobs = "";
  for (let i = 0; i < 9; i++) blobs += `<ellipse cx="${R() * 1200}" cy="${R() * 800}" rx="${80 + R() * 160}" ry="${50 + R() * 90}" fill="${i % 2 ? "#ffffff" : "#d6c2f0"}" opacity="${0.35 + R() * 0.35}" style="animation:float ${10 + R() * 8}s ease-in-out ${R() * 5}s infinite"/>`;
  return `<svg viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice" width="100%" height="100%" aria-hidden="true">
    <defs><filter id="bl${id}"><feGaussianBlur stdDeviation="30"/></filter></defs><g filter="url(#bl${id})">${blobs}</g>
    <text x="620" y="760" font-family="Instrument Serif" font-style="italic" font-size="240" fill="#8a52a8" opacity=".1">hush</text></svg>`;
}

const COVER_SPOTS = [
  { l: 4, t: 6, w: 46, r: -6, d: 0 }, { l: 54, t: 0, w: 32, r: 5, d: 1.2 }, { l: 42, t: 42, w: 40, r: -3, d: 2.4 },
  { l: 0, t: 60, w: 30, r: 7, d: 0.8 }, { l: 76, t: 50, w: 22, r: -8, d: 3 }, { l: 24, t: 36, w: 18, r: 4, d: 1.8 },
];
const FACE_SPOTS = [{ l: 78, t: 14, w: 17 }, { l: 30, t: 78, w: 15 }, { l: 86, t: 82, w: 12 }];
const CHAR_SPOT = { night: "left:58%;top:70%", volume: "left:64%;top:-2%", lowend: "left:62%;top:72%", soft: "left:6%;top:-8%" };

function renderWorlds() {
  const cl = PROFILE.clusters;
  $("#worlds-lede").textContent = `${cl.length} sides of how you listen, found by grouping your songs on how they sound. They blur into each other at the edges — more spectrum than boxes.`;
  $("#worlds-body").innerHTML = cl.map((c, i) => {
    const key = THEME_OF[c.id], t = THEMES[key], tr = c.traits;
    const traits = ["tempo_feel", "energy", "valence", "low_end_share", "speechiness"].filter((k) => tr[k]).map((k, j) => {
      const label = { tempo_feel: "tempo", energy: "energy", valence: "mood", low_end_share: "bass", speechiness: "vocals" }[k];
      const v = k === "tempo_feel" ? tr[k].p50 : tr[k].mean;
      return `<li style="--r:${[-2, 1.5, -1, 2, -1.5][j]}deg"><b>${esc(WORDS[k](v))}</b><span>${label} ${k === "tempo_feel" ? "" : v.toFixed(2)}</span></li>`;
    }).join("");
    const covers = c.examples.filter((e) => e.cover).slice(0, 6).map((e, j) => {
      const s = COVER_SPOTS[j];
      return `<a class="piece" href="${esc(e.spotify_url)}" target="_blank" rel="noopener" style="left:${s.l}%;top:${s.t}%;width:${s.w}%;aspect-ratio:1;--r:${s.r}deg;--d:${s.d}s;--t:${9 + j}s;z-index:${6 - j}">
        <img loading="lazy" src="${img(e.cover)}" alt="Cover: ${esc(e.title)} by ${esc(e.artists[0] || "")}"><span class="lbl">${esc(e.title)} — ${esc(e.artists[0] || "")}${e.estimated ? " ≈" : ""}</span></a>`;
    }).join("");
    const faces = c.artists.filter((a) => a.photo).slice(0, 3).map((a, j) => {
      const s = FACE_SPOTS[j];
      return `<div class="face" style="left:${s.l}%;top:${s.t}%;width:${s.w}%;aspect-ratio:1;--d:${j}s;z-index:${7 + j}" data-tip="${esc(`<b>${a.name}</b>${Math.round(a.share * 100)}% of this world`)}"><img loading="lazy" src="${img(a.photo)}" alt=""><span class="nm">${esc(a.name)}</span></div>`;
    }).join("");
    const tint = `--c-a:${t.tint.a};--c-b:${t.tint.b};--c-ink:${t.tint.ink};--c-line:${t.tint.line}`;
    return `<article class="world${i % 2 ? " flip" : ""}" data-theme="${key}" aria-labelledby="w${c.id}">
      <div class="motif">${motif(key, c.id)}</div>
      <div class="copy reveal">
        <div class="kicker mono"><span>World ${pad(i + 1)} / ${pad(cl.length)}</span><span>— ${t.kicker}</span></div>
        <p class="share outline" aria-label="${Math.round(c.share * 100)} percent of your listening">${Math.round(c.share * 100)}<small>%</small></p>
        <h3 id="w${c.id}">${t.lines[0]}<em>${t.lines[1]}</em></h3>
        <p class="auto">of your listening · ${c.tracks} songs · ${Math.round(c.estimated_share * 100)}% estimated from clips<br>measured as: ${esc(c.label)}</p>
        <ul class="traits">${traits}</ul>
        <p class="genres">${esc(c.genres.slice(0, 6).map((g) => g[0]).join(" / "))}</p>
        <p class="who">${esc(c.artists.map((a) => a.name).join(" · "))}</p>
      </div>
      <div class="collage stagger">${covers}${faces}<div class="character" style="${CHAR_SPOT[key]};${tint}">${CAST[t.char]()}</div></div>
    </article>`;
  }).join("");
  watch($("#worlds-body"));
}

/* ---------------------------------------------------------------- fingerprint: the print */
const FP_TRAITS = [
  ["energy", "energy", 0, 1], ["danceability", "groove", 0, 1], ["valence", "mood", 0, 1], ["acousticness", "acoustic", 0, 0.6],
  ["speechiness", "vocals", 0, 0.35], ["low_end_share", "bass", 0.4, 1], ["tempo_feel", "tempo", 75, 150], ["loudness", "loudness", -14, -2],
];
function renderPrint() {
  const T = PROFILE.traits, N = FP_TRAITS.length, C = 250;
  const raw = FP_TRAITS.map(([k, , lo, hi]) => clamp(((k === "tempo_feel" ? T[k].p50 : T[k].mean) - lo) / (hi - lo)));
  const avg = raw.reduce((a, b) => a + b, 0) / raw.length;
  const vals = raw.map((v) => clamp(0.5 + (v - avg) * 2.4));
  const f = (th) => {
    const u = ((((th + Math.PI / 2) / (2 * Math.PI)) % 1) + 1) % 1 * N, i = Math.floor(u) % N, t = u - Math.floor(u);
    const s = (1 - Math.cos(t * Math.PI)) / 2;
    return vals[i] * (1 - s) + vals[(i + 1) % N] * s;
  };
  const R = rand(7);
  let rings = "";
  const RINGS = 18;
  for (let k = 0; k < RINGS; k++) {
    const base = 24 + k * 9, amp = Math.pow(k / RINGS, 1.2) * 54, gap = 0.18 + R() * 0.35, a0 = R() * Math.PI * 2;
    let d = "";
    for (let th = a0; th <= a0 + Math.PI * 2 - gap; th += 0.035) {
      const r = base + amp * (f(th) - 0.45) + Math.sin(th * 3 + k) * 1.4;
      d += `${d ? "L" : "M"}${(C + Math.cos(th) * r).toFixed(1)} ${(C + Math.sin(th) * r).toFixed(1)}`;
    }
    rings += `<path class="ring${k === RINGS - 4 ? " hi" : ""}" d="${d}" style="transition-delay:${k * 0.06}s"/>`;
  }
  const outer = (th) => 24 + (RINGS - 1) * 9 + 54 * Math.pow((RINGS - 1) / RINGS, 1.2) * (f(th) - 0.45);
  const labels = FP_TRAITS.map(([k, name], i) => {
    const th = -Math.PI / 2 + (i / N) * Math.PI * 2, ro = outer(th) + 4;
    const x0 = C + Math.cos(th) * ro, y0 = C + Math.sin(th) * ro, x1 = C + Math.cos(th) * 228, y1 = C + Math.sin(th) * 228;
    const anchor = Math.abs(Math.cos(th)) < 0.3 ? "middle" : Math.cos(th) > 0 ? "start" : "end";
    const v = k === "tempo_feel" ? T[k].p50 : T[k].mean, word = WORDS[k](v);
    const tipTxt = `<b>${name}: ${word}</b>typical ${k === "loudness" ? v.toFixed(1) + " dB" : k === "tempo_feel" ? Math.round(v) + " BPM" : v.toFixed(2)}<br>most songs ${T[k].p10.toFixed(k === "tempo_feel" ? 0 : 2)} – ${T[k].p90.toFixed(k === "tempo_feel" ? 0 : 2)}`;
    return `<g data-tip="${esc(tipTxt)}" tabindex="0"><line class="lead" x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}"/><circle class="dot" cx="${x0}" cy="${y0}" r="4"/>
      <text class="tlabel" x="${x1}" y="${y1 + (y1 < C ? -22 : 8)}" text-anchor="${anchor}">${name}</text>
      <text class="tword" x="${x1}" y="${y1 + (y1 < C ? -2 : 30)}" text-anchor="${anchor}">${esc(word)}</text>
      <circle class="hit" cx="${x1}" cy="${y1}" r="30"/></g>`;
  }).join("");
  $("#fp-print").innerHTML = `<figcaption><h3>The <em>print</em></h3><p>Each ring bends toward what your music leans on.</p></figcaption>
    <svg class="print-svg draw" viewBox="-60 -40 620 580" role="img" aria-label="Sound fingerprint: ${FP_TRAITS.map(([k, n]) => `${n} ${WORDS[k](k === "tempo_feel" ? T[k].p50 : T[k].mean)}`).join(", ")}">
      <g class="rings">${rings}</g>${labels}
      <text x="250" y="256" text-anchor="middle" font-family="Instrument Serif" font-style="italic" font-size="22" fill="var(--paper)">you</text></svg>`;
}

/* ---------------------------------------------------------------- fingerprint: tempo terrain */
function renderTerrain() {
  const L = PROFILE.landscapes, [b0, b1] = PROFILE.landscape_bpm, W = 640, padL = 130, padR = 16, H = 92, step = 52;
  const top = 26, height = top + (L.length - 1) * step + H + 40;
  const x = (i, n) => padL + (i / (n - 1)) * (W - padL - padR);
  let ridges = "";
  L.forEach((l, j) => {
    const base = top + j * step + H, w = worldOf(l.cluster), n = l.points.length;
    let d = `M${padL} ${base}`;
    l.points.forEach((p, i) => (d += ` L${x(i, n).toFixed(1)} ${(base - p * H).toFixed(1)}`));
    d += ` L${W - padR} ${base}`;
    ridges += `<path d="${d} Z" fill="var(--ink)"/><path class="ridge" d="${d}" fill="none" stroke="${w ? w.mark : "var(--paper)"}"/>
      <line x1="${padL - 10}" y1="${base}" x2="${padL - 2}" y2="${base}" stroke="${w ? w.mark : "var(--paper)"}" stroke-width="2"/>
      <text class="rlabel" x="${padL - 16}" y="${base + 5}" text-anchor="end">${esc(w ? w.name : `World ${j + 1}`)}</text>`;
  });
  const ticks = [60, 80, 100, 120, 140, 160, 180, 200].map((b) => { const tx = padL + ((b - b0) / (b1 - b0)) * (W - padL - padR); return `<line x1="${tx}" y1="${height - 30}" x2="${tx}" y2="${height - 24}"/><text x="${tx}" y="${height - 10}" text-anchor="middle">${b}</text>`; }).join("");
  $("#fp-terrain").innerHTML = `<figcaption><h3>Tempo <em>terrain</em></h3><p>Where your BPMs pile up — one ridge per world.</p></figcaption>
    <svg class="terrain" viewBox="0 0 ${W} ${height}" role="img" aria-label="Tempo distribution per world">${ridges}
      <g class="axis"><line x1="${padL}" y1="${height - 30}" x2="${W - padR}" y2="${height - 30}"/>${ticks}<text x="${padL - 16}" y="${height - 10}" text-anchor="end">BPM →</text></g>
      <line class="cross" x1="0" y1="${top - 10}" x2="0" y2="${height - 30}"/>
      <rect class="hover-pad" x="${padL}" y="0" width="${W - padL - padR}" height="${height - 30}"/></svg>`;
  const svg = $("#fp-terrain svg"), cross = $(".cross", svg), padEl = $(".hover-pad", svg);
  padEl.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect(), vx = ((e.clientX - r.left) / r.width) * W;
    const frac = clamp((vx - padL) / (W - padL - padR)), bpm = Math.round(b0 + frac * (b1 - b0));
    cross.setAttribute("x1", vx); cross.setAttribute("x2", vx); cross.style.opacity = 0.6;
    const rows = L.map((l) => { const w = worldOf(l.cluster), v = l.points[Math.round(frac * (l.points.length - 1))]; return { name: w ? w.name : "", v }; }).sort((a, b) => b.v - a.v);
    showTip(`<b>${bpm} BPM</b>${rows.map((rw) => `${esc(rw.name)} ${"▮".repeat(Math.round(rw.v * 8)) || "·"}`).join("<br>")}`, e.clientX, e.clientY);
  });
  padEl.addEventListener("pointerleave", () => { cross.style.opacity = 0; hideTip(); });
}

/* ---------------------------------------------------------------- fingerprint: artist orbit */
function renderOrbit() {
  const A = PROFILE.artists, smax = Math.max(...A.map((a) => a.share)), C = 250, GOLD = 2.39996;
  const planets = A.map((a, i) => {
    const rr = 92 + Math.pow(1 - a.share / smax, 0.7) * 128, th = i * GOLD - 1.2, x = C + Math.cos(th) * rr, y = C + Math.sin(th) * rr;
    const s = 14 + 30 * Math.sqrt(a.share / smax);
    return `<g class="planet" data-tip="${esc(`<b>${a.name}</b>${Math.round(a.share * 100)}% of your listening`)}" tabindex="0">
      <g class="counter" style="transform-origin:${x}px ${y}px">
        <clipPath id="cp${i}"><circle cx="${x}" cy="${y}" r="${s}"/></clipPath>
        ${a.photo ? `<image href="${img(a.photo)}" x="${x - s}" y="${y - s}" width="${s * 2}" height="${s * 2}" clip-path="url(#cp${i})" preserveAspectRatio="xMidYMid slice"/>` : `<circle cx="${x}" cy="${y}" r="${s}" fill="var(--ink-3)"/>`}
        <circle class="face-ring" cx="${x}" cy="${y}" r="${s + 3}"/>
        ${i < 4 ? `<text class="name" x="${x}" y="${y + s + 22}" text-anchor="middle">${esc(a.name)}</text>` : ""}
      </g></g>`;
  }).join("");
  $("#fp-orbit").innerHTML = `<figcaption><h3>Artist <em>orbit</em></h3><p>Closer and bigger = more of your listening.</p></figcaption>
    <svg class="orbit" viewBox="-30 -30 560 560" role="img" aria-label="Your top artists: ${esc(A.map((a) => `${a.name} ${Math.round(a.share * 100)}%`).join(", "))}">
      ${[100, 160, 220].map((r) => `<circle class="track-ring" cx="250" cy="250" r="${r}"/>`).join("")}
      <g class="spinner">${planets}</g>
      <circle cx="250" cy="250" r="44" fill="var(--paper)"/><text class="you" x="250" y="259" text-anchor="middle">you</text></svg>`;
}

/* ---------------------------------------------------------------- fingerprint: genre constellation */
function renderStars() {
  const G = PROFILE.genres.slice(0, 12), E = PROFILE.genre_links.filter((e) => G.some((g) => g[0] === e.a) && G.some((g) => g[0] === e.b));
  const W = 680, H = 460, gmax = G[0][1], R = rand(3);
  const nodes = G.map(([name, share], i) => ({ name, share, x: W / 2 + Math.cos(i * 2.4) * 140 + R() * 10, y: H / 2 + Math.sin(i * 2.4) * 120 + R() * 10 }));
  const idx = Object.fromEntries(nodes.map((n, i) => [n.name, i]));
  for (let it = 0; it < 400; it++) {   // tiny deterministic force layout
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
  const edges = E.map((e) => { const a = nodes[idx[e.a]], b = nodes[idx[e.b]]; return `<line class="edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke-opacity="${(0.1 + e.w * 0.5).toFixed(2)}" stroke-width="${(0.8 + e.w * 1.4).toFixed(2)}"/>`; }).join("");
  const stars = nodes.map((n, i) => {
    const r = 3 + 11 * Math.sqrt(n.share / gmax);
    return `<g class="s" data-tip="${esc(`<b>${n.name}</b>${Math.round(n.share * 100)}% of your genre mix`)}" tabindex="0">
      <circle class="halo" cx="${n.x}" cy="${n.y}" r="${r * 2.6}" style="animation-delay:${(i * 0.37) % 4}s"/><circle class="star" cx="${n.x}" cy="${n.y}" r="${r}"/>
      <text class="slabel" x="${n.x + r + 7}" y="${n.y + 5}">${esc(n.name)}</text></g>`;
  }).join("");
  $("#fp-stars").innerHTML = `<figcaption><h3>Genre <em>constellation</em></h3><p>Lines join genres your artists share.</p></figcaption>
    <svg class="stars" viewBox="0 0 ${W} ${H}" role="img" aria-label="Your genres: ${esc(G.map((g) => `${g[0]} ${Math.round(g[1] * 100)}%`).join(", "))}">${edges}${stars}</svg>`;
}

function renderText() {
  const T = PROFILE.traits;
  const rows = FP_TRAITS.map(([k, n]) => `<tr><td>${n}</td><td>${esc(WORDS[k](k === "tempo_feel" ? T[k].p50 : T[k].mean))}</td><td>${(k === "tempo_feel" ? T[k].p50 : T[k].mean).toFixed(2)}</td><td>${T[k].p10.toFixed(2)} – ${T[k].p90.toFixed(2)}</td></tr>`).join("");
  const worlds = PROFILE.clusters.map((c) => `<tr><td>${esc(worldOf(c.id).name)}</td><td>${Math.round(c.share * 100)}%</td><td>${c.tracks}</td><td>${esc(c.artists.slice(0, 4).map((a) => a.name).join(", "))}</td></tr>`).join("");
  $("#fp-text div").innerHTML = `<table><tr><th>Trait</th><th>In words</th><th>Typical</th><th>Most songs</th></tr>${rows}</table>
    <table><tr><th>World</th><th>Share</th><th>Songs</th><th>Leading artists</th></tr>${worlds}</table>
    <table><tr><th>Artist</th><th>Share of listening</th></tr>${PROFILE.artists.map((a) => `<tr><td>${esc(a.name)}</td><td>${Math.round(a.share * 100)}%</td></tr>`).join("")}</table>`;
}

/* ---------------------------------------------------------------- notes */
function renderNotes() {
  const c = PROFILE.coverage;
  $("#notes-body").innerHTML = `
    <div class="note"><h4>Recorded from</h4><p class="big">${c.tracks}</p><p>songs from your Spotify — top lists across three time ranges, recent plays and your saved library. Duplicate releases merged.</p></div>
    <div class="note"><h4>Measured</h4><p class="big">${c.with_traits - c.estimated}<span class="hand" style="font-size:26px"> + ${c.estimated}≈</span></p><p>${c.with_traits - c.estimated} songs with official measurements; ${c.estimated} estimated from 30-second previews and calibrated against the official ones. Bass measured straight from preview audio for ${c.with_bass}.</p></div>
    <div class="note"><h4>How picks are scored</h4><p>Sound (fit to a world), scene (genre overlap) and connection (how strongly your own artists point there), mixed on the desk. <span class="est-mark">≈</span> estimated · <b>~</b> scene borrowed from the artists that led there.</p></div>
    <div class="note"><h4>Fine print</h4><p>World separation ${PROFILE.separation}: your worlds overlap a lot. Genres for ${c.artists_with_genres} artists. Sources: Spotify · ReccoBeats · Deezer · MusicBrainz.</p></div>
    <div class="note redig"><h4>Fresh pressing</h4><p>Re-pull your Spotify listening and dig new candidates. Anything already measured is kept, so it's quicker than the first run.</p>
      <button type="button" class="ticket" id="redig">Re-dig my library <span>↻</span></button></div>`;
  $("#redig").addEventListener("click", async () => {
    const st = await fetch("/api/pipeline/start?refresh=1", { method: "POST" }).then((r) => r.json());
    startPlant(st);
  });
  $("#notes-character").innerHTML = `<div style="--c-a:var(--lavender);--c-b:var(--cream)">${CAST.digger()}</div>`;
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  // Nothing analysed yet, or a run in progress: show the pressing plant instead.
  const st = await fetch("/api/pipeline").then((r) => r.json());
  if (!st.ready || st.running) return startPlant(st);
  PROFILE = await fetch("/api/profile").then((r) => r.json());
  THEME_OF = assignThemes(PROFILE.clusters);
  $("#issue").textContent = `Issue ${PROFILE.meta.date.split("-").reverse().join(".")} · ${PROFILE.meta.songs} songs`;
  setupDesk();
  renderWorlds(); renderPrint(); renderTerrain(); renderOrbit(); renderStars(); renderText(); renderNotes();
  watch();
  await loadRecs();
}
boot();
