// Multi-user screens: sign the guestbook (sign in / create an account), bring your records (connect a listening
// source), and a small account strip. Only shown by the multi-user server; dashboard.py never asks for them.
import { useState, type FormEvent, type ReactNode } from "react";
import {
  ApiError, connectLastfm, connectSpotify, deleteAccount, setConsent, signIn, signOut, signUp, startRun, uploadHistory,
} from "../../services/api";
import type { PipelineStatus } from "../../types/api";
import "../../styles/account.css";

const message = (e: unknown) => (e instanceof ApiError ? e.message : "Couldn't reach the server. Try again in a moment.");

function Card({ kicker, title, children }: { kicker: string; title: ReactNode; children: ReactNode }) {
  return (
    <section className="acct-card">
      <p className="mono acct-kicker">{kicker}</p>
      <h1 className="acct-title">{title}</h1>
      {children}
    </section>
  );
}

export function SignIn({ onDone }: { onDone: () => void }) {
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await (mode === "in" ? signIn(email, password) : signUp(email, password, name || undefined));
      onDone();
    } catch (err) { setError(message(err)); } finally { setBusy(false); }
  }

  return (
    <main className="acct">
      <Card kicker="A1 · Frequency" title={<>Sign the <em>guestbook</em></>}>
        <form className="acct-form" onSubmit={submit}>
          {mode === "up" && (
            <label><span className="mono">Name (optional)</span>
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="nickname" maxLength={80} /></label>
          )}
          <label><span className="mono">Email</span>
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" /></label>
          <label><span className="mono">Password</span>
            <input type="password" required minLength={mode === "up" ? 10 : 1} value={password}
              onChange={(e) => setPassword(e.target.value)} autoComplete={mode === "up" ? "new-password" : "current-password"} /></label>
          {mode === "up" && <p className="acct-hint">At least 10 characters.</p>}
          {error && <p className="acct-error" role="alert">{error}</p>}
          <button className="acct-button" disabled={busy}>{mode === "in" ? "Come in" : "Create my account"}</button>
        </form>
        <p className="acct-switch">
          {mode === "in" ? "New here? " : "Already signed? "}
          <button type="button" className="acct-link" onClick={() => { setMode(mode === "in" ? "up" : "in"); setError(null); }}>
            {mode === "in" ? "Create an account" : "Sign in instead"}
          </button>
        </p>
      </Card>
    </main>
  );
}

export function Sources({ st, onStarted, onSignedOut }: { st: PipelineStatus; onStarted: () => void; onSignedOut: () => void }) {
  const avail = st.sources?.available ?? { spotify: false, lastfm: false, history_upload: true };
  const connected = new Set((st.sources?.connected ?? []).map((c) => c.provider));
  const [user, setUser] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try { await fn(); onStarted(); } catch (err) { setError(message(err)); } finally { setBusy(null); }
  }

  return (
    <main className="acct">
      <Card kicker={`Signed in as ${st.account?.display_name || st.account?.email || ""}`} title={<>Bring your <em>records</em></>}>
        <p className="acct-lede">Frequency learns your taste from what you actually play. Pick one way in; you can add another later.</p>
        <div className="acct-sources">
          <div className="acct-source">
            <h2>Spotify</h2>
            <p>Your top songs, recent plays and saved songs.</p>
            {avail.spotify_note && <p className="acct-hint">{avail.spotify_note}</p>}
            <button className="acct-button" disabled={!avail.spotify || busy !== null}
              onClick={() => run("spotify", connected.has("spotify") ? () => startRun("spotify") : connectSpotify)}>
              {connected.has("spotify") ? "Analyse again" : avail.spotify ? "Connect Spotify" : "Not set up here"}
            </button>
          </div>
          <div className="acct-source">
            <h2>Last.fm</h2>
            <p>Your public scrobbles: top songs, recent plays, loved songs.</p>
            <form onSubmit={(e) => { e.preventDefault(); void run("lastfm", async () => { await connectLastfm(user.trim()); await startRun("lastfm"); }); }}>
              <label><span className="mono">Last.fm username</span>
                <input value={user} onChange={(e) => setUser(e.target.value)} disabled={!avail.lastfm} pattern="[A-Za-z0-9_.\-]+" required /></label>
              <button className="acct-button" disabled={!avail.lastfm || busy !== null}>{avail.lastfm ? "Use my scrobbles" : "Not set up here"}</button>
            </form>
          </div>
          <div className="acct-source">
            <h2>Listening history file</h2>
            <p>Request <b>Extended streaming history</b> on Spotify's privacy page, then upload the zip (or its JSON files) here.
              It's read once, then deleted; only your listening counts are kept.</p>
            <label className="acct-button acct-file">
              {busy === "upload" ? "Reading…" : "Upload history"}
              <input type="file" accept=".zip,.json,application/zip,application/json" disabled={busy !== null}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) void run("upload", () => uploadHistory(f)); e.target.value = ""; }} />
            </label>
          </div>
        </div>
        {error && <p className="acct-error" role="alert">{error}</p>}
        <AccountDetails onSignedOut={onSignedOut} />
      </Card>
    </main>
  );
}

function AccountDetails({ onSignedOut }: { onSignedOut: () => void }) {
  const [pw, setPw] = useState("");
  const [consent, setConsentState] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <details className="acct-details">
      <summary className="mono">Your account</summary>
      <label className="acct-check">
        <input type="checkbox" checked={!!consent} onChange={async (e) => {
          const v = e.target.checked;
          try { await setConsent(v); setConsentState(v); } catch (err) { setError(message(err)); }
        }} />
        <span>Let songs from my library help calibrate clip measurements for everyone (only song measurements are used,
          never who listened; you can withdraw any time).</span>
      </label>
      <button className="acct-link" onClick={async () => { await signOut(); onSignedOut(); }}>Sign out</button>
      <form className="acct-delete" onSubmit={async (e) => {
        e.preventDefault();
        if (!confirm("Delete your account and everything Frequency stores about your listening? This can't be undone.")) return;
        try { await deleteAccount(pw); onSignedOut(); } catch (err) { setError(message(err)); }
      }}>
        <label><span className="mono">Password to delete account</span>
          <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" required /></label>
        <button className="acct-link acct-danger">Delete my account</button>
      </form>
      {error && <p className="acct-error" role="alert">{error}</p>}
    </details>
  );
}

/** A quiet strip above the dashboard: who's signed in, their sources, sign out. */
export function AccountBar({ st, onSources, onSignedOut }: { st: PipelineStatus; onSources: () => void; onSignedOut: () => void }) {
  return (
    <div className="acct-bar mono">
      <span>{st.account?.display_name || st.account?.email}</span>
      <button className="acct-link" onClick={onSources}>Sources</button>
      <button className="acct-link" onClick={async () => { await signOut(); onSignedOut(); }}>Sign out</button>
    </div>
  );
}
