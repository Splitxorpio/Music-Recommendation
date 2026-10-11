import { describe, expect, it } from "vitest";
import { layoutStars } from "../components/fingerprint/Stars";
import { recommendationQuery } from "../services/api";
import type { PipelineStatus, Progress, Stage } from "../types/api";
import { clockText, fmtClock, keepPolling, overall, plantStatus } from "./progress";
import { assignThemes, makeWorldOf } from "./themes";
import { because, issueDate, vivid } from "./util";

describe("worlds", () => {
  it("gives each sub-taste its best theme, each theme once", () => {
    const themes = assignThemes([
      { id: 0, label: "dark, produced, melodic" },
      { id: 1, label: "upbeat, high-energy, loud" },
      { id: 2, label: "rap-heavy, quieter, danceable" },
      { id: 3, label: "acoustic, laid-back, light on bass" },
    ]);
    expect(themes).toEqual({ 0: "night", 1: "volume", 2: "lowend", 3: "soft" });
    expect(makeWorldOf(themes)(2)?.name).toBe("Low end theory");
    expect(makeWorldOf(themes)(9)).toBeNull();
  });
});

describe("recommendation request", () => {
  it("matches what the desk always sent", () => {
    const base = { ws: 50, wc: 25, wn: 25, known: false, far: false, bpm: null, bass: null };
    expect(recommendationQuery(base)).toBe("ws=0.5&wc=0.25&wn=0.25&known=0&far=0&n=25");
    expect(recommendationQuery({ ...base, ws: 80, known: true, bpm: [99, 194], bass: [0.03, 0.86] }))
      .toBe("ws=0.8&wc=0.25&wn=0.25&known=1&far=0&n=25&bpm=99%2C194&bass=0.03%2C0.86");
  });
});

const stages: Stage[] = [
  { key: "a", side: "A1", title: "", lines: ["", ""], weight: 1, blurb: "" },
  { key: "b", side: "A2", title: "", lines: ["", ""], weight: 3, blurb: "" },
];
const progress = (over: Partial<Progress> = {}): Progress => ({
  status: "running", stage: "b", now: null, error: null, started: 1000, updated: 1100, covers: [], facts: [],
  stages: { a: { state: "done", done: 0, total: null, label: null }, b: { state: "active", done: 1, total: 2, label: null } },
  ...over,
});
const status = (p: Progress, running: boolean, ready = false): PipelineStatus => ({ progress: p, stages, running, ready });

describe("pressing plant", () => {
  it("weights progress by stage", () => {
    expect(overall(progress(), stages)).toBeCloseTo((1 + 3 * 0.5) / 4);
    const noTotal = progress({ stages: { a: { state: "done", done: 0, total: null, label: null }, b: { state: "active", done: 5, total: null, label: null } } });
    expect(overall(noTotal, stages)).toBeCloseTo((1 + 3 * 0.25) / 4);
  });

  it("reads a run whose process died as paused", () => {
    expect(plantStatus(status(progress(), false), false)).toBe("paused");
    expect(plantStatus(status(progress(), true), false)).toBe("running");
    expect(plantStatus(status(progress({ status: "error" }), false), false)).toBe("error");
    expect(plantStatus(status(progress({ status: "error" }), false), true)).toBe("done");
  });

  it("formats the clock and time left", () => {
    expect(fmtClock(400)).toBe("006:40");
    expect(clockText(progress({ started: null }), "running", 0.5)).toBe("000:00");
    expect(clockText(progress(), "running", 0.5, 1400)).toBe("006:40 · ~7 min left");
    expect(clockText(progress(), "running", 0.02, 1400)).toBe("006:40 · warming up");
    expect(clockText(progress(), "paused", 0.5, 9999)).toBe("001:40");
  });

  it("stops polling only once a finished run is ready", () => {
    expect(keepPolling(status(progress({ status: "done" }), false, true))).toBe(false);
    expect(keepPolling(status(progress({ status: "paused" }), false, true))).toBe(true);
    expect(keepPolling(status(progress(), true, false))).toBe(true);
  });
});

describe("helpers", () => {
  it("formats dates and reasons", () => {
    expect(issueDate("2026-10-07")).toBe("07.10.2026");
    expect(because({ reason: "Because you play slayr · fits your side" })).toBe("Because you play slayr");
  });

  it("keeps sampled colours vivid and mid-light", () => {
    // values from the original vanilla-JS version
    expect(vivid([0, 0, 0])).toEqual([164, 132, 132]);
    expect(vivid([200, 30, 30])).toEqual([227, 69, 69]);
    expect(vivid([12, 200, 90])).toEqual([53, 243, 132]);
  });

  it("lays the genre stars out the same way every time", () => {
    const genres: [string, number][] = [["hip hop", 0.3], ["trap", 0.2], ["rage", 0.1]];
    const links = [{ a: "hip hop", b: "trap", w: 1 }, { a: "trap", b: "pop", w: 0.5 }];
    const a = layoutStars(genres, links), b = layoutStars(genres, links);
    expect(a.nodes).toEqual(b.nodes);
    expect(a.edges).toHaveLength(1);                  // links to genres outside the top 12 are dropped
  });
});

import { csrfToken } from "../services/api";

describe("csrfToken", () => {
  it("reads the readable CSRF cookie among others", () => {
    expect(csrfToken("a=1; freq_csrf=abc%2Fdef; b=2")).toBe("abc/def");
    expect(csrfToken("freq_csrf=xyz")).toBe("xyz");
    expect(csrfToken("other=1")).toBe("");
  });
});
