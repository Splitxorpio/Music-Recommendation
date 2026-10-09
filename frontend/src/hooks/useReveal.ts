import { useEffect, useRef, useState } from "react";

// One shared observer for every .reveal / .stagger / .draw element: once it scrolls into view it gets
// the "in" class (and keeps it), which starts its CSS entrance transition.
type Cb = () => void;
const callbacks = new Map<Element, Cb>();
let io: IntersectionObserver | null = null;

function observer() {
  io ??= new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) {
      callbacks.get(e.target)?.();
      callbacks.delete(e.target);
      io!.unobserve(e.target);
    }
  }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
  return io;
}

/** Returns a ref for the element and " in" once it has been seen (append it to the className). */
export function useReveal<T extends Element>() {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    callbacks.set(el, () => setSeen(true));
    observer().observe(el);
    return () => { callbacks.delete(el); io?.unobserve(el); };
  }, [seen]);
  return [ref, seen ? " in" : ""] as const;
}
