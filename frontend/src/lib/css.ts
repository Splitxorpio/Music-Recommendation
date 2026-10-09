import type { CSSProperties } from "react";

/** Inline style that can carry CSS custom properties, e.g. vars({ "--r": "2deg" }). */
export const vars = (v: Record<string, string | number | undefined>, extra: CSSProperties = {}): CSSProperties =>
  ({ ...extra, ...v }) as CSSProperties;
