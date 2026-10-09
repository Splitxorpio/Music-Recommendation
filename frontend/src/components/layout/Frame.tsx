// Pieces of the page frame that never change.

/** Shared SVG filters: the hand-drawn wobble every illustration and doodle runs through. */
export function SvgDefs() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true">
      <defs>
        <filter id="wobble"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2" seed="4" /><feDisplacementMap in="SourceGraphic" scale="2.6" /></filter>
        <filter id="wobble-strong"><feTurbulence type="fractalNoise" baseFrequency="0.02" numOctaves="2" seed="9" /><feDisplacementMap in="SourceGraphic" scale="5" /></filter>
      </defs>
    </svg>
  );
}

export function Colophon() {
  return (
    <footer className="colophon">
      <span>Frequency</span><span className="mono">pressed locally at 127.0.0.1 · nothing leaves this machine</span><a href="#top" className="mono">back to the top ↑</a>
    </footer>
  );
}
