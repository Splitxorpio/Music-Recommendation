// The one floating note that follows the pointer. Anything can show it through useTip(); the content is
// ordinary React (no HTML strings), so song titles from outside services are always rendered as text.
import { createContext, useContext, useLayoutEffect, useMemo, useRef, useState, type FocusEvent, type PointerEvent, type ReactNode } from "react";
import { clamp } from "../../lib/util";

interface TipApi { show: (content: ReactNode, x: number, y: number) => void; hide: () => void }
const TipContext = createContext<TipApi>({ show: () => {}, hide: () => {} });

export function TooltipProvider({ children }: { children: ReactNode }) {
  const el = useRef<HTMLDivElement>(null);
  const pos = useRef({ x: 0, y: 0 });
  const [content, setContent] = useState<ReactNode>(null);
  const [on, setOn] = useState(false);

  const place = () => {
    const t = el.current;
    if (!t) return;
    const w = t.offsetWidth, h = t.offsetHeight, { x, y } = pos.current;
    t.style.left = `${clamp(x + 14, 8, innerWidth - w - 8)}px`;
    t.style.top = `${clamp(y - h - 12, 8, innerHeight - h - 8)}px`;
  };
  useLayoutEffect(place, [content]);           // new content: measure it, then place

  const api = useMemo<TipApi>(() => ({
    show(c, x, y) {
      pos.current = { x, y };
      setOn(true);
      setContent((prev: ReactNode) => (prev === c ? prev : c));
      place();
    },
    hide() { setOn(false); },
  }), []);

  return (
    <TipContext.Provider value={api}>
      {children}
      <div className={`tooltip${on ? " on" : ""}`} id="tooltip" role="status" aria-live="polite" ref={el}>{content}</div>
    </TipContext.Provider>
  );
}

export const useTooltip = () => useContext(TipContext);

/** Pointer and keyboard handlers that show `content` for the element they're spread on. */
export function useTip() {
  const tip = useTooltip();
  return (content: ReactNode) => ({
    onPointerMove: (e: PointerEvent) => tip.show(content, e.clientX, e.clientY),
    onPointerLeave: () => tip.hide(),
    onFocus: (e: FocusEvent) => { const r = e.currentTarget.getBoundingClientRect(); tip.show(content, r.left + r.width / 2, r.top); },
    onBlur: () => tip.hide(),
  });
}
