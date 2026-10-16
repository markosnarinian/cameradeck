import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { request, message, onUnauthorized } from "./api";
import type { Status } from "./types";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./components/ui/dialog";
import { Power } from "lucide-react";
import { CameraPage } from "./Camera";
import { Library } from "./Library";
import { Transfer } from "./Transfer";
import "./style.css";

function App() {
  const [auth, setAuth] = useState<
      "checking" | "locked" | "ready" | "unreachable"
    >("checking"),
    [required, setRequired] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [status, setStatus] = useState<Status | null>(null),
    [connected, setConnected] = useState(false),
    [busy, setBusy] = useState(false),
    [password, setPassword] = useState(""),
    [epoch, setEpoch] = useState(0);
  const [powerOpen, setPowerOpen] = useState(false),
    [powerAction, setPowerAction] = useState("shutdown"),
    [powerConfirm, setPowerConfirm] = useState(""),
    [powerError, setPowerError] = useState(""),
    [powerBusy, setPowerBusy] = useState(false),
    [powerPending, setPowerPending] = useState("");
  const session = useCallback(async () => {
    setAuth("checking");
    setError("");
    try {
      const s = await request("/api/session");
      setRequired(s.required);
      setAuth(s.authenticated || !s.required ? "ready" : "locked");
    } catch (e) {
      setAuth("unreachable");
      setError(message(e));
    }
  }, []);
  useEffect(() => {
    onUnauthorized(() => {
      setAuth("locked");
      setStatus(null);
      setConnected(false);
    });
    void session();
    return () => onUnauthorized(() => {});
  }, [session]);
  useEffect(() => {
    if (auth !== "ready" || powerPending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>,
      stopped = false;
    const poll = async () => {
      try {
        const data = await request("/api/status", undefined, {
          signal: controller.signal,
        });
        if (!stopped) {
          setStatus(data);
          setConnected(true);
          setError("");
        }
      } catch (e) {
        if (!stopped) {
          setConnected(false);
          setError(message(e));
        }
      } finally {
        if (!stopped) timer = setTimeout(poll, 1200);
      }
    };
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [auth, epoch, powerPending]);
  const run = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setNotice("");
    try {
      await fn();
      setEpoch((e) => e + 1);
    } catch (e) {
      setNotice(message(e));
    } finally {
      setBusy(false);
    }
  };
  if (powerPending)
    return (
      <main className="auth">
        <h1>CameraDeck</h1>
        <h2 role="status">
          {powerPending === "shutdown"
            ? "Pi is shutting down…"
            : "Pi is rebooting…"}
        </h2>
        <p>
          {powerPending === "shutdown"
            ? "Physical access is required to turn the Pi back on."
            : "Wait for the Pi to restart, then reload this page to reconnect."}
        </p>
        <Button onClick={() => window.location.reload()}>Reload page</Button>
      </main>
    );
  if (auth !== "ready")
    return (
      <main className="auth">
        <h1>CameraDeck</h1>
        {auth === "checking" ? (
          <p role="status">Checking session…</p>
        ) : auth === "unreachable" ? (
          <>
            <h2>Pi unreachable</h2>
            <p role="alert">{error}</p>
            <Button onClick={session}>Retry connection</Button>
          </>
        ) : (
          <>
            <h2>Locked</h2>
            <form
              autoComplete="off"
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  await request("/api/login", { password });
                  setPassword("");
                  await session();
                });
              }}
            >
              <label className="field">
                Password
                <Input
                  type="password"
                  name="camera-access"
                  autoComplete="off"
                  data-1p-ignore
                  data-lpignore="true"
                  data-bwignore="true"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  autoFocus
                />
              </label>
              <Button disabled={busy}>Sign in</Button>
            </form>
            {notice && <p role="alert">{notice}</p>}
          </>
        )}
      </main>
    );
  const seq = status?.sequence;
  return (
    <>
      <header>
        <NavLink to="/" className="brand">
          CameraDeck
        </NavLink>
        <nav>
          <NavLink to="/" end>
            Camera
          </NavLink>
          <NavLink to="/library">Library</NavLink>
          <NavLink to="/transfer">Transfer</NavLink>
        </nav>
        <div className="header-actions">
          <Button
            variant="ghost"
            onClick={() => {
              setPowerConfirm("");
              setPowerError("");
              setPowerOpen(true);
            }}
          >
            <Power size={16} /> Power
          </Button>
          {required && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await request("/api/logout", {});
                  setAuth("locked");
                  setStatus(null);
                  setConnected(false);
                })
              }
            >
              Lock
            </Button>
          )}
        </div>
      </header>
      <Dialog
        open={powerOpen}
        onOpenChange={(open) => {
          if (!powerBusy) setPowerOpen(open);
        }}
      >
        <DialogContent closeDisabled={powerBusy}>
          <DialogTitle>Pi power</DialogTitle>
          <DialogDescription>
            Stop capture and transfers and finish any downloads or deletions
            first. Shutting down requires physical access to turn the Pi back
            on.
          </DialogDescription>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (powerBusy || powerConfirm.toLowerCase() !== powerAction)
                return;
              setPowerBusy(true);
              setPowerError("");
              try {
                await request("/api/system/power", {
                  action: powerAction,
                  confirm: powerConfirm,
                });
                setPowerPending(powerAction);
              } catch (e) {
                setPowerError(message(e));
              } finally {
                setPowerBusy(false);
              }
            }}
          >
            <label className="field">
              Action
              <select
                value={powerAction}
                disabled={powerBusy}
                onChange={(e) => {
                  setPowerAction(e.target.value);
                  setPowerConfirm("");
                  setPowerError("");
                }}
              >
                <option value="shutdown">Shut down</option>
                <option value="reboot">Reboot</option>
              </select>
            </label>
            <label className="field">
              Type {powerAction.toUpperCase()} to confirm
              <Input
                value={powerConfirm}
                disabled={powerBusy}
                autoComplete="off"
                onChange={(e) => setPowerConfirm(e.target.value)}
              />
            </label>
            {seq?.active && (
              <p role="alert">
                Stop the capture sequence before changing Pi power.
              </p>
            )}
            {powerError && (
              <p className="error" role="alert">
                {powerError}
              </p>
            )}
            <div className="viewer-actions">
              <Button
                type="button"
                variant="outline"
                disabled={powerBusy}
                onClick={() => setPowerOpen(false)}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={
                  powerBusy ||
                  busy ||
                  !connected ||
                  !!seq?.active ||
                  powerConfirm.toLowerCase() !== powerAction
                }
              >
                {powerBusy
                  ? "Sending…"
                  : powerAction === "shutdown"
                    ? "Shut down Pi"
                    : "Reboot Pi"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      <main>
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
        {status?.error && (
          <div className="notice error" role="alert">
            {status.error}
          </div>
        )}
        {status?.low_space && status.free_bytes != null && (
          <div className="notice warning" role="alert">
            Low disk space: {(status.free_bytes / 1024 ** 3).toFixed(1)} GB
            free. Capture stops below 256 MB — transfer and remove captures
            soon.
          </div>
        )}
        {seq && (seq.active || seq.completed > 0 || seq.error) && (
          <div className="sequence">
            <span>
              {seq.active ? "Capturing" : "Stopped"} · {seq.completed} photos
              {seq.total > 0 ? ` / ${seq.total}` : ""}
              {seq.rate != null
                ? ` · ${seq.rate.toFixed(2)} photos/s achieved${seq.settings?.rate != null && seq.rate < seq.settings.rate * 0.95 ? ` (target ${seq.settings.rate})` : ""}`
                : " · achieved rate unavailable"}
              {seq.skipped > 0 ? ` · ${seq.skipped} skipped` : ""}
              {seq.error ? ` · ${seq.error}` : ""}
              {!connected ? " · last known state" : ""}
            </span>
            {seq.active && (
              <Button
                variant="destructive"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await request("/api/sequence/stop", {});
                  })
                }
              >
                Stop
              </Button>
            )}
          </div>
        )}
        {notice && (
          <div className="notice" role="status">
            {notice}
            <Button variant="ghost" onClick={() => setNotice("")}>
              Dismiss
            </Button>
          </div>
        )}
        <Routes>
          <Route
            path="/"
            element={
              <CameraPage
                status={status}
                connected={connected}
                run={run}
                busy={busy}
              />
            }
          />
          <Route
            path="/library"
            element={<Library capturing={!!seq?.active} />}
          />
          <Route
            path="/transfer"
            element={<Transfer capturing={!!seq?.active} />}
          />
          <Route
            path="*"
            element={
              <p>
                Page not found. <NavLink to="/">Camera</NavLink>
              </p>
            }
          />
        </Routes>
      </main>
    </>
  );
}
createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);
