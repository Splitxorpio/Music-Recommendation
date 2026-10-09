import { useEffect, useState } from "react";

const SECTIONS = [["crate", "01", "Next"], ["desk", "02", "Desk"], ["worlds", "03", "Worlds"], ["fingerprint", "04", "Fingerprint"], ["notes", "05", "Notes"]];

export function Masthead({ issue }: { issue: string }) {
  const [scrolled, setScrolled] = useState(false);
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    const onScroll = () => setScrolled(scrollY > 40);
    addEventListener("scroll", onScroll, { passive: true });
    return () => removeEventListener("scroll", onScroll);
  }, []);

  // Highlight the section crossing the middle of the screen.
  useEffect(() => {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) setActive(e.target.id);
    }, { rootMargin: "-45% 0px -50% 0px" });
    for (const [id] of SECTIONS) { const el = document.getElementById(id); if (el) io.observe(el); }
    return () => io.disconnect();
  }, []);

  return (
    <header className={`masthead${scrolled ? " scrolled" : ""}`} id="top">
      <a className="wordmark" href="#top" aria-label="Frequency, back to top"><span>Frequency</span><em>est. by your ears</em></a>
      <nav aria-label="Sections">
        {SECTIONS.map(([id, n, label]) => (
          <a key={id} href={`#${id}`} className={active === id ? "active" : undefined}><i>{n}</i>{label}</a>
        ))}
      </nav>
      <p className="issue" id="issue">{issue}</p>
    </header>
  );
}
