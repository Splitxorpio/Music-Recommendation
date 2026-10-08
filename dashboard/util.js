// Shared helpers for the dashboard and the loading screen.

/* ---------------------------------------------------------------- utilities */
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const img = (u) => (u ? `/img?u=${encodeURIComponent(u)}` : "");
export const pad = (n) => String(n).padStart(2, "0");
export const clamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
export const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
export const rand = (seed) => () => ((seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296);
export const because = (r) => (r.reason || "").split(" · ")[0];

/* ---------------------------------------------------------------- artwork colour */
const colourCache = new Map();
export function sampleColour(url) {
  if (!url) return Promise.resolve(null);
  if (colourCache.has(url)) return colourCache.get(url);
  const p = new Promise((resolve) => {
    const im = new Image();
    im.onload = () => {
      const c = document.createElement("canvas");
      c.width = c.height = 24;
      const x = c.getContext("2d", { willReadFrequently: true });
      x.drawImage(im, 0, 0, 24, 24);
      const d = x.getImageData(0, 0, 24, 24).data;
      let r = 0, g = 0, b = 0, w = 0;
      for (let i = 0; i < d.length; i += 4) {
        const mx = Math.max(d[i], d[i + 1], d[i + 2]), mn = Math.min(d[i], d[i + 1], d[i + 2]);
        const sat = mx ? (mx - mn) / mx : 0, light = (mx + mn) / 510;
        const wt = 0.05 + sat * sat * (1 - Math.abs(light - 0.55) * 1.4);
        r += d[i] * wt; g += d[i + 1] * wt; b += d[i + 2] * wt; w += wt;
      }
      resolve(vivid([r / w, g / w, b / w]).join(" "));
    };
    im.onerror = () => resolve(null);
    im.src = img(url);
  });
  colourCache.set(url, p);
  return p;
}
// Keep sampled colours glowing on charcoal: at least moderately saturated, mid-light.
export function vivid([r, g, b]) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l0 = (mx + mn) / 2, d = mx - mn;
  let h = 0;
  if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  h = (h * 60 + 360) % 360;
  const s0 = d ? d / (1 - Math.abs(2 * l0 - 1)) : 0;
  const s = d ? Math.max(s0, 0.55) : 0.15, l = clamp(l0, 0.58, 0.7);
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  const [r1, g1, b1] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [r1, g1, b1].map((v) => Math.round((v + m) * 255));
}
export const hexRGB = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).join(" ");

