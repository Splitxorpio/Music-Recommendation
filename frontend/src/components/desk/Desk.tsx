// The mixing desk: weights, reach switches and BPM/bass ranges for the crate.
import { useState } from "react";
import { useReveal } from "../../hooks/useReveal";
import type { DigSettings, Profile } from "../../types/api";

export const DEFAULTS: DigSettings = { ws: 50, wc: 25, wn: 25, known: false, far: false, bpm: null, bass: null };

interface Props {
  settings: DigSettings;
  onChange: (patch: Partial<DigSettings>) => void;
  onReset: () => void;
  resetCount: number;
  ranges: Profile["ranges"];
  status: { busy: boolean; text: string };
}

const FADERS = [["ws", "Sound", "Sound match weight"], ["wc", "Scene", "Scene match weight"], ["wn", "Connection", "Connection weight"]] as const;

export function Desk({ settings, onChange, onReset, resetCount, ranges, status }: Props) {
  const [ref, inCls] = useReveal<HTMLElement>();
  return (
    <section className={`desk reveal${inCls}`} id="desk" aria-labelledby="desk-title" ref={ref}>
      <div className="desk-head">
        <h2 id="desk-title"><span className="outline">Tune</span> <em>the</em> dig</h2>
        <p className="hand">turn a dial, the crate reshuffles <svg className="doodle-arrow" viewBox="0 0 90 40" aria-hidden="true"><path d="M2 30 C 25 6, 55 4, 82 18 M70 8 L84 18 L70 26" /></svg></p>
      </div>
      <form className="panel" id="panel" onSubmit={(e) => e.preventDefault()}>
        <span className="screw s1"></span><span className="screw s2"></span><span className="screw s3"></span><span className="screw s4"></span>
        <fieldset className="faders">
          <legend>Mix</legend>
          {FADERS.map(([k, cap, label]) => (
            <label className="fader" key={k}>
              <span className="cap">{cap}</span>
              <input type="range" min="0" max="100" step="5" data-w={k} aria-label={label}
                value={settings[k]} onChange={(e) => onChange({ [k]: +e.target.value })} />
              <output data-o={k}>{settings[k]}</output>
            </label>
          ))}
        </fieldset>
        <fieldset className="switches">
          <legend>Reach</legend>
          <label className="switch"><input type="checkbox" id="opt-known" checked={settings.known} onChange={(e) => onChange({ known: e.target.checked })} /><span className="track"><span className="knob"></span></span><span className="sw-text"><b>Familiar artists</b><small>unheard songs from artists you already play</small></span></label>
          <label className="switch"><input type="checkbox" id="opt-far" checked={settings.far} onChange={(e) => onChange({ far: e.target.checked })} /><span className="track"><span className="knob"></span></span><span className="sw-text"><b>Two steps out</b><small>artists related to your related artists</small></span></label>
        </fieldset>
        <fieldset className="rails">
          <legend>Filter</legend>
          {/* keyed by reset count: reset puts both thumbs back at the ends */}
          <Rail key={`bpm${resetCount}`} name="bpm" cap="BPM" range={ranges.bpm} step={1} fmt={(v) => String(Math.round(v))} onChange={(bpm) => onChange({ bpm })} />
          <Rail key={`bass${resetCount}`} name="bass" cap="Bass" range={ranges.bass} step={0.01} fmt={(v) => v.toFixed(2)} onChange={(bass) => onChange({ bass })} />
        </fieldset>
        <div className="status">
          <span className={`lamp${status.busy ? " busy" : ""}`} id="lamp"></span><span id="status-text">{status.text}</span>
          <button type="button" className="reset" id="reset" onClick={onReset}>reset</button>
        </div>
      </form>
    </section>
  );
}

interface RailProps {
  name: "bpm" | "bass"; cap: string; range: [number, number]; step: number;
  fmt: (v: number) => string; onChange: (v: [number, number] | null) => void;
}

/** Two thumbs on one track. Thumbs may cross; the range is always low → high, and null when wide open. */
function Rail({ name, cap, range: [min, max], step, fmt, onChange }: RailProps) {
  const [ends, setEnds] = useState({ lo: min, hi: max });
  let a = ends.lo, b = ends.hi;
  if (a > b) [a, b] = [b, a];

  const move = (end: "lo" | "hi", v: number) => {
    const next = { ...ends, [end]: v };
    setEnds(next);
    const x = Math.min(next.lo, next.hi), y = Math.max(next.lo, next.hi);
    onChange(x <= min && y >= max ? null : [x, y]);
  };

  return (
    <div className="rail" data-rail={name}><span className="cap">{cap}</span>
      <div className="dual">
        <input type="range" data-end="lo" aria-label={`Minimum ${name === "bpm" ? "BPM" : "bass"}`} min={min} max={max} step={step} value={ends.lo} onChange={(e) => move("lo", +e.target.value)} />
        <input type="range" data-end="hi" aria-label={`Maximum ${name === "bpm" ? "BPM" : "bass"}`} min={min} max={max} step={step} value={ends.hi} onChange={(e) => move("hi", +e.target.value)} />
        <span className="fill" style={{ left: `${((a - min) / (max - min)) * 100}%`, right: `${100 - ((b - min) / (max - min)) * 100}%` }}></span>
      </div>
      <output className="led" data-led={name}>{`${fmt(a)} – ${fmt(b)}`}</output>
    </div>
  );
}
