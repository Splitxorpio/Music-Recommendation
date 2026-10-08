// The pressing plant: the loading screen shown while your library is analysed.
// A record store after hours. A turntable plays the run, the sleeve-back tracklist shows each stage,
// and real covers drop into the crate as they're found.
import { CAST } from "./characters.js";
import { $, $$, esc, img, clamp, REDUCED, sampleColour } from "./util.js";

const CHAR_FOR = { login: "listener", pull: "digger", notes: "digger", previews: "listener", lowend: "walker",
                   calibrate: "dreamer", genres: "digger", fingerprint: "dancer", crates: "digger", sleeves: "dancer" };
const BULLETS = ["★", "→", "✶", "♪", "✳", "~"];
const MAX_CRATE = 26;

let el, stages = [], timer = 0, lastCover = null, shownCovers = new Set(), shownFacts = "", charKey = null, finishing = false;

function shell() {
  el = document.createElement("section");
  el.className = "plant";
  el.id = "plant";
  el.setAttribute("aria-live", "polite");
  el.innerHTML = `
    <div class="plant-amb" aria-hidden="true"></div>
    <header class="plant-top">
      <span class="plant-mark">Frequency</span>
      <span class="mono plant-sub">the pressing plant · open after hours</span>
      <span class="mono plant-clock" id="pl-clock" aria-label="Elapsed time">000:00</span>
    </header>
    <div class="plant-grid">
      <div class="plant-copy">
        <p class="mono plant-kicker" id="pl-kicker"></p>
        <h1 class="plant-title" id="pl-title"></h1>
        <p class="plant-blurb" id="pl-blurb"></p>
        <p class="plant-now mono" id="pl-now"><span class="eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span id="pl-now-text"></span></p>
        <div class="plant-count" id="pl-count"></div>
        <div class="plant-action" id="pl-action"></div>
        <ul class="plant-facts" id="pl-facts"></ul>
      </div>
      <div class="deck" aria-hidden="true">
        <div class="plinth">
          <span class="plinth-brand mono">Frequency · model 33</span>
          <div class="platter"><div class="record" id="pl-record"><div class="grooves"></div><div class="label" id="pl-label"><span>side a</span><b>Frequency</b><span>33⅓</span></div></div><div class="sheen"></div></div>
          <svg class="tonearm" id="pl-arm" viewBox="0 0 120 360"><circle cx="60" cy="40" r="26" class="arm-base"/><circle cx="60" cy="40" r="9" class="arm-pivot"/>
            <path d="M60 40 L60 250 Q60 292 40 318" class="arm-tube"/><rect x="26" y="306" width="30" height="40" rx="4" transform="rotate(28 41 326)" class="arm-head"/></svg>
          <div class="knob"><i></i></div>
          <span class="led" id="pl-led"></span><span class="speed mono">33 · 45</span>
        </div>
        <div class="deck-character" id="pl-char"></div>
      </div>
      <aside class="sleeve-back" aria-label="Analysis stages">
        <p class="mono sb-head">Tracklist <span id="pl-pct"></span></p>
        <ol class="sb-list" id="pl-list"></ol>
        <p class="sb-foot mono">Pressed from your Spotify · ReccoBeats · Deezer · iTunes · MusicBrainz</p>
      </aside>
    </div>
    <div class="crate-wrap"><p class="mono crate-label" id="pl-crate-label">the crate</p><div class="plant-crate" id="pl-crate"></div></div>`;
  document.body.appendChild(el);
  document.body.classList.add("pressing");
}

/* ---------------------------------------------------------------- progress maths */
function overall(p) {
  let sum = 0, got = 0;
  for (const s of stages) {
    const st = p.stages[s.key] || {};
    sum += s.weight;
    if (st.state === "done") got += s.weight;
    else if (st.state === "active") got += s.weight * (st.total ? clamp(st.done / st.total) : 0.25);
  }
  return sum ? got / sum : 0;
}
const fmtClock = (sec) => `${String(Math.floor(sec / 60)).padStart(3, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;

/* ---------------------------------------------------------------- render */
function render(st) {
  const p = st.progress, status = finishing ? "done" : p.status === "running" && !st.running ? "paused" : (st.running ? "running" : p.status);
  const frac = status === "done" ? 1 : overall(p);
  const active = stages.find((s) => s.key === p.stage) || null;
  const ast = active ? p.stages[active.key] : null;
  el.dataset.status = status;

  // turntable: needle rests off the record until a run starts, then tracks inward with progress
  const playing = status === "running";
  $("#pl-record").classList.toggle("spinning", playing && !REDUCED);
  $("#pl-led").classList.toggle("on", playing);
  $("#pl-arm").style.transform = `rotate(${status === "idle" || status === "paused" || status === "error" ? -9 : 12 + frac * 20}deg)`;
  $("#pl-pct").textContent = status === "idle" ? "" : `${Math.round(frac * 100)}%`;

  // clock + time left
  if (p.started && status !== "idle") {
    const end = status === "running" ? Date.now() / 1000 : p.updated || Date.now() / 1000;
    const el2 = Math.max(0, end - p.started);
    let left = "";
    if (status === "running") left = frac > 0.04 && el2 > 30 ? ` · ~${Math.max(1, Math.round((el2 * (1 - frac)) / frac / 60))} min left` : " · warming up";
    $("#pl-clock").textContent = fmtClock(el2) + left;
  } else $("#pl-clock").textContent = "000:00";

  // copy
  const title = $("#pl-title"), kicker = $("#pl-kicker"), blurb = $("#pl-blurb"), action = $("#pl-action");
  if (status === "idle") {
    kicker.textContent = "nothing on the turntable yet";
    title.innerHTML = `<span class="l1">Your records</span><span class="l2">aren't pressed yet.</span>`;
    blurb.textContent = "Drop the needle and Frequency will pull your Spotify listening, listen to every song it can find, and dig for what to play next.";
    action.innerHTML = `<button class="ticket" id="pl-go" type="button">Drop the needle <span>↘ side a</span></button>
      <ul class="plant-terms hand"><li>takes about 10–15 minutes the first time</li><li>you'll sign in to Spotify once</li><li>you can close this tab, just keep the program running and the lid open</li></ul>`;
  } else if (status === "paused" || status === "error") {
    kicker.textContent = active ? `stopped at ${active.side} · ${active.title.toLowerCase()}` : "the needle lifted";
    title.innerHTML = `<span class="l1">The needle</span><span class="l2">lifted.</span>`;
    blurb.textContent = p.error ? "" : "The run stopped before the end, probably because the program was closed. Everything measured so far is saved.";
    action.innerHTML = `${p.error ? `<p class="plant-error hand">${esc(p.error)}</p>` : ""}<button class="ticket" id="pl-go" type="button">Put the needle back <span>↻ resume</span></button>`;
  } else if (status === "done") {
    kicker.textContent = "side b · run out groove";
    title.innerHTML = `<span class="l1">Pressed.</span><span class="l2">Here's your record.</span>`;
    blurb.textContent = "";
    action.innerHTML = "";
  } else if (active) {
    kicker.textContent = `side ${active.side[0].toLowerCase()} · track ${active.side[1]} of ${stages.filter((s) => s.side[0] === active.side[0]).length}`;
    const html = `<span class="l1">${esc(active.lines[0])}</span><span class="l2">${esc(active.lines[1])}</span>`;
    if (title.dataset.k !== active.key) { title.innerHTML = html; title.dataset.k = active.key; title.classList.remove("swap"); void title.offsetWidth; title.classList.add("swap"); }
    blurb.textContent = active.blurb;
    action.innerHTML = active.key === "login" ? `<p class="plant-login hand">→ A Spotify tab just opened. Click <b>Agree</b>, then come back here.</p>` : "";
  }
  if (status !== "running") title.dataset.k = "";
  const go = $("#pl-go");
  if (go) go.onclick = start;

  // now playing + counter
  $("#pl-now").style.visibility = playing && p.now ? "visible" : "hidden";
  $("#pl-now-text").textContent = p.now || "";
  $("#pl-count").innerHTML = playing && ast && (ast.done || ast.total)
    ? `<b>${ast.done ?? 0}</b><span>${ast.total ? `/ ${ast.total}` : ""}</span><small>${esc(ast.label || "")}</small>` : "";

  // tracklist
  $("#pl-list").innerHTML = stages.map((s) => {
    const x = p.stages[s.key] || {}, state = status === "done" ? "done" : x.state;
    const f = state === "done" ? 1 : state === "active" && x.total ? clamp(x.done / x.total) : 0;
    const side = s.side[0] === "B" && s.side[1] === "1" ? `<li class="sb-side mono">side b</li>` : s.side === "A1" ? `<li class="sb-side mono">side a</li>` : "";
    return `${side}<li class="sb-track ${state}" style="--f:${f}"><span class="mono n">${s.side}</span><span class="t">${esc(s.title)}</span>
      <span class="mono c">${state === "done" ? "✓" : state === "active" ? (x.total ? `${x.done}/${x.total}` : "•••") : ""}</span><span class="groove"></span></li>`;
  }).join("");

  // character for the current stage
  const want = status === "idle" || status === "paused" || status === "error" ? "listener" : status === "done" ? "dancer" : CHAR_FOR[p.stage] || "digger";
  if (want !== charKey) { $("#pl-char").innerHTML = CAST[want](); charKey = want; }

  // facts in the margin, newest first
  const factsKey = JSON.stringify(p.facts.map((f) => f.text));
  if (factsKey !== shownFacts) {
    shownFacts = factsKey;
    $("#pl-facts").innerHTML = [...p.facts].reverse().slice(0, 6).map((f, i) =>
      `<li class="${i === 0 ? "fresh" : ""}" style="--r:${[-1.5, 1, -0.5, 1.5, -1, 0.5, -2, 1][i % 8]}deg"><span class="b">${BULLETS[i % BULLETS.length]}</span>${esc(f.text)}</li>`).join("");
  }

  // covers drop into the crate; the newest one tints the record label and the room
  const fresh = p.covers.filter((c) => !shownCovers.has(c.url));
  const crate = $("#pl-crate");
  for (const c of fresh) {
    shownCovers.add(c.url);
    const d = document.createElement("figure");
    d.className = "crate-cover";
    d.style.setProperty("--r", `${(Math.random() * 14 - 7).toFixed(1)}deg`);
    d.innerHTML = `<img src="${img(c.url)}" alt="${esc(c.title)} by ${esc(c.artist)}">${c.tag ? `<span class="sticker known">${esc(c.tag)}</span>` : ""}<figcaption class="mono">${esc(c.title)}<br><span>${esc(c.artist)}</span></figcaption>`;
    crate.appendChild(d);
    while (crate.children.length > MAX_CRATE) crate.firstElementChild.remove();
  }
  $("#pl-crate-label").textContent = shownCovers.size ? `the crate · ${shownCovers.size} sleeves so far` : "the crate · empty until your songs turn up";
  const latest = p.covers[p.covers.length - 1];
  if (latest && latest.url !== lastCover) {
    lastCover = latest.url;
    sampleColour(latest.url).then((rgb) => rgb && el.style.setProperty("--amb", rgb));
  }

  if (status === "done" && !finishing) finish();
}

/* ---------------------------------------------------------------- actions */
async function poll() {
  try {
    const st = await fetch("/api/pipeline").then((r) => r.json());
    stages = st.stages;
    render(st);
    if (st.ready && !st.running && st.progress.status !== "error" && st.progress.status !== "paused" && st.progress.status !== "idle") return;
  } catch { /* server restarting; keep trying */ }
  timer = setTimeout(poll, 1000);
}

async function start() {
  const go = $("#pl-go");
  if (go) { go.disabled = true; go.querySelector("span").textContent = "…"; }
  const st = await fetch("/api/pipeline/start", { method: "POST" }).then((r) => r.json());
  stages = st.stages;
  render(st);
  clearTimeout(timer);
  timer = setTimeout(poll, 800);
}

function finish() {
  finishing = true;
  clearTimeout(timer);
  el.classList.add("lifting");                        // record lifts off, the room fades, the dashboard is next
  setTimeout(() => location.reload(), REDUCED ? 300 : 2200);
}

export function startPlant(st) {
  if (!el) shell();
  stages = st.stages;
  scrollTo(0, 0);
  render(st);
  clearTimeout(timer);
  if (st.running || st.progress.status === "running") timer = setTimeout(poll, 1000);
}
