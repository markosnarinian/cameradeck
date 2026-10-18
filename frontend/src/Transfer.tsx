import { useCallback, useEffect, useState } from "react";
import { Play, Square, Trash2 } from "lucide-react";
import { message, request } from "./api";
import type { RsyncStatus } from "./types";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "./components/ui/dialog";

const STATE_LABELS: Record<RsyncStatus["state"], string> = {
  idle: "Not run since CameraDeck started",
  running: "Running",
  stopping: "Stopping",
  succeeded: "Finished",
  failed: "Failed",
  cancelled: "Stopped",
};

function duration(seconds: number) {
  const h = Math.floor(seconds / 3600),
    m = Math.floor((seconds % 3600) / 60),
    s = seconds % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

function PiRsync({ capturing }: { capturing: boolean }) {
  const [rsync, setRsync] = useState<RsyncStatus | null>(null),
    [destination, setDestination] = useState<string | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const data: RsyncStatus = await request("/api/transfer/rsync");
      setRsync(data);
      setDestination((current) => current ?? data.destination);
    } catch (e) {
      setError(message(e));
    }
  }, []);
  const active = !!rsync?.active;
  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, active ? 1000 : 5000);
    return () => clearInterval(timer);
  }, [refresh, active]);
  const act = async (path: string, body: unknown) => {
    setBusy(true);
    setError("");
    try {
      setRsync({ ...rsync!, ...(await request(path, body)) });
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const job = rsync?.job,
    progress = job?.progress;
  return (
    <section className="transfer-section" aria-labelledby="pi-rsync-heading">
      <h2 id="pi-rsync-heading">rsync</h2>
      <p>
        The Pi pushes the same files to an SSH destination such as{" "}
        <code>faros@192.168.1.10:/mnt/wd2tb/faros</code>, or to a directory on a
        drive mounted on the Pi. Capture is blocked while it runs.
      </p>
      <p className="muted">The SSH key must already be set up.</p>
      <form
        className="rsync-run"
        onSubmit={(e) => {
          e.preventDefault();
          void act("/api/transfer/rsync/start", { destination });
        }}
      >
        <label className="rsync-destination-field">
          Destination
          <Input
            value={destination ?? ""}
            onChange={(e) => setDestination(e.target.value)}
            placeholder="user@host:/path/"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            disabled={active}
          />
        </label>
        {active ? (
          <Button
            type="button"
            variant="destructive"
            disabled={busy || rsync?.state === "stopping"}
            onClick={() => void act("/api/transfer/rsync/stop", {})}
          >
            <Square /> Stop rsync
          </Button>
        ) : (
          <Button
            type="submit"
            disabled={busy || !rsync || capturing || !destination?.trim()}
            title={
              capturing ? "Stop capture before starting rsync." : undefined
            }
          >
            <Play /> Start rsync
          </Button>
        )}
      </form>
      {capturing && !active && (
        <p className="muted">Stop capture before starting rsync.</p>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {rsync && (
        <div className="operation-progress rsync-status" role="status">
          <span>
            rsync: {STATE_LABELS[rsync.state]}
            {job && ` · ${duration(job.elapsed)}`}
            {job && !active && job.message && ` · ${job.message}`}
          </span>
          {job && (
            <>
              <progress
                aria-label="rsync progress"
                value={progress?.percent}
                max={100}
              />
              <small>
                {progress
                  ? [
                      `${progress.percent}%`,
                      `${progress.transferred} transferred`,
                      progress.rate,
                      active && `ETA ${progress.eta}`,
                      progress.files !== null &&
                        `${progress.files} files copied`,
                      progress.scanning &&
                        "still listing files; total may grow",
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : active
                    ? "Connecting and listing files…"
                    : "No progress reported."}
              </small>
              <small className="rsync-destination">To {job.destination}</small>
              <details>
                <summary>Command and log</summary>
                <pre className="rsync-log" aria-label="rsync log">
                  {[`$ ${job.command}`, ...job.log].join("\n")}
                </pre>
              </details>
            </>
          )}
        </div>
      )}
      {active && (
        <p className="muted">
          rsync keeps running if you leave this page. Restarting or updating
          CameraDeck stops it.
        </p>
      )}
    </section>
  );
}

export function Transfer({ capturing }: { capturing: boolean }) {
  const [deleteOpen, setDeleteOpen] = useState(false),
    [confirmation, setConfirmation] = useState(""),
    [deleting, setDeleting] = useState(false),
    [deleteError, setDeleteError] = useState(""),
    [feedback, setFeedback] = useState("");
  const [deleteProgress, setDeleteProgress] = useState<{
    active: boolean;
    completed: number;
    total: number | null;
    eta_seconds: number | null;
  } | null>(null);
  useEffect(() => {
    if (!deleting) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const progress = await request("/api/media/delete-all/progress");
        if (!cancelled && progress.active) setDeleteProgress(progress);
      } catch {
        // Losing a progress poll must not interrupt the deletion request.
        if (!cancelled) setDeleteProgress(null);
      } finally {
        if (!cancelled) timer = setTimeout(refresh, 1000);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [deleting]);
  const removeAll = async () => {
    setDeleteProgress(null);
    setDeleting(true);
    setDeleteError("");
    setFeedback("");
    try {
      const result: { deleted: string[]; missing: string[] } = await request(
        "/api/media/delete-all",
        { confirm: confirmation },
        { timeout: 0 },
      );
      setFeedback(
        `Deleted ${result.deleted.length} captures from the Pi.` +
          (result.missing.length
            ? ` ${result.missing.length} unavailable captures could not be deleted. Check the Library.`
            : " Remote copies were not changed."),
      );
      setDeleteOpen(false);
    } catch (e) {
      setDeleteError(message(e));
    } finally {
      setDeleting(false);
    }
  };
  return (
    <>
      <div className="heading">
        <h1>Transfer</h1>
      </div>
      <PiRsync capturing={capturing} />
      <section
        className="transfer-section"
        aria-labelledby="delete-all-heading"
      >
        <h2 id="delete-all-heading">Free up space</h2>
        <p>
          After verifying your remote copies, permanently delete all Library
          photos and videos from the Pi, including originals, metadata,
          thumbnails, and previews. Remote copies and CameraDeck settings are
          not changed.
        </p>
        <Button
          variant="destructive"
          disabled={capturing || deleting}
          onClick={() => {
            setConfirmation("");
            setDeleteError("");
            setDeleteOpen(true);
          }}
        >
          <Trash2 /> Delete all media
        </Button>
        {capturing && (
          <p className="muted">Stop capture before deleting all media.</p>
        )}
        {feedback && <p role="status">{feedback}</p>}
      </section>
      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          if (!deleting) setDeleteOpen(open);
        }}
      >
        <DialogContent
          closeDisabled={deleting}
          onEscapeKeyDown={(e) => {
            if (deleting) e.preventDefault();
          }}
          onPointerDownOutside={(e) => {
            if (deleting) e.preventDefault();
          }}
        >
          <DialogTitle>Delete all media?</DialogTitle>
          <DialogDescription>
            This permanently removes every Library photo and video from the Pi,
            not just selected items. This cannot be undone. Verify that your
            transfer finished and your remote copies are complete first.
          </DialogDescription>
          <p>
            CameraDeck must be idle. This does not verify or delete remote
            copies.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (
                confirmation.toLowerCase() === "delete all" &&
                !deleting &&
                !capturing
              )
                void removeAll();
            }}
          >
            <label className="field">
              Type DELETE ALL to confirm
              <Input
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                disabled={deleting}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            {deleteError && (
              <div className="error" role="alert">
                {deleteError}
              </div>
            )}
            <div className="viewer-actions">
              <Button
                type="button"
                variant="outline"
                disabled={deleting}
                onClick={() => setDeleteOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="destructive"
                disabled={
                  deleting ||
                  capturing ||
                  confirmation.toLowerCase() !== "delete all"
                }
              >
                {deleting ? "Deleting…" : "Permanently delete all media"}
              </Button>
            </div>
            {deleting && (
              <div className="operation-progress" role="status">
                <span>
                  Deleting all Library media…
                  {deleteProgress?.total != null &&
                    ` · ${deleteProgress.completed.toLocaleString()} / ${deleteProgress.total.toLocaleString()} captures deleted`}
                </span>
                <progress
                  aria-label="Deleting all Library media"
                  value={deleteProgress?.total ? deleteProgress.completed : undefined}
                  max={deleteProgress?.total || 1}
                />
                <small>
                  {deleteProgress?.eta_seconds != null
                    ? `ETA: ${duration(Math.ceil(deleteProgress.eta_seconds))} remaining (estimated)`
                    : "ETA: estimating…"}
                  {" · Keep this page open until finished."}
                </small>
              </div>
            )}
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
