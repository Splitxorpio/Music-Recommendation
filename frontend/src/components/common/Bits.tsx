// Small shared pieces of pick display.
import type { CSSProperties, ReactNode } from "react";
import { vars } from "../../lib/css";
import type { Pick } from "../../types/api";
import { Character, type CastName } from "./characters";
import { useTip } from "./Tooltip";

const r = (n: number | null | undefined) => Math.round(n ?? 0);
const scene = (p: Pick["parts"]) => (p.scene == null ? "?" : String(Math.round(p.scene)));

/** "Sound 86 · Scene 79 · Connection 80". */
export const partsLine = (p: Pick["parts"], inferredMark = "") =>
  `Sound ${r(p.sound)} · Scene ${p.scene == null ? "?" : scene(p) + inferredMark} · Connection ${r(p.connection)}`;

export function Stickers({ pick }: { pick: Pick }) {
  const tip = useTip();
  return (
    <>
      {pick.mode === "known" && <span className="sticker known" style={vars({ "--r": "2deg" })}>familiar</span>}
      {pick.hop === 2 && <span className="sticker far" style={vars({ "--r": "-3deg" })}>2 steps out</span>}
      {pick.estimated && (
        <span className="sticker est" style={vars({ "--r": "3deg" })} {...tip("Measured from a 30-second preview, then calibrated")}>≈ estimated</span>
      )}
    </>
  );
}

export function Empty({ character = "digger", style, children }: { character?: CastName; style?: CSSProperties; children: ReactNode }) {
  return (
    <div className="empty" style={style}>
      <div><Character name={character} /></div>
      <p>{children}</p>
    </div>
  );
}
