import { useCallback, useEffect, useState } from "react";
import { Check, Copy, Play, Square } from "lucide-react";
import { message, request } from "./api";
import { Library } from "./Library";
import type { RsyncStatus } from "./types";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";

const RSYNC_COMMAND = `rsync -r --whole-file --info=progress2,stats2 --human-readable --ignore-existing \\
  --exclude='*.preview.jpg' \\
  --exclude='*.thumb.jpg' \\
  --exclude='.*' \\
  --exclude='*.tmp' \\
  --exclude='*.pending.*' \\
  pi@192.168.1.14:/home/pi/cameradeck/media/ \\
  "/Volumes/WD APFS/Faros-OTA/boxed/"`;

// The device UI is served over plain HTTP, where navigator.clipboard is absent.
async function copyText(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    area.remove();
  }
}

function CopyButton({
  text,
  label = "Copy",
  className,
}: {
  text: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState<boolean | null>(null);
  useEffect(() => {
    if (copied === null) return;
    const timer = setTimeout(() => setCopied(null), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <Button
      className={className}
      variant="outline"
      onClick={() =>
        copyText(text).then(
          () => setCopied(true),
          () => setCopied(false),
        )
      }
      aria-live="polite"
    >
      {copied ? <Check size={16} /> : <Copy size={16} />}
      {copied ? "Copied" : copied === false ? "Copy failed" : label}
    </Button>
  );
}

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
      <h2 id="pi-rsync-heading">rsync from CameraDeck</h2>
      <p>
        The Pi pushes the same files to an SSH destination such as{" "}
        <code>user@mac.local:/Volumes/WD APFS/Faros-OTA/boxed/</code>, or to a
        directory on a drive mounted on the Pi. Capture is blocked while it
        runs.
      </p>
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
            title={capturing ? "Stop capture before starting rsync." : undefined}
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
                      progress.files !== null && `${progress.files} files copied`,
                      progress.scanning && "still listing files; total may grow",
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
      <details className="rsync-setup">
        <summary>SSH setup</summary>
        <p>
          The destination must allow key login for this Pi without a password
          prompt. On a Mac, enable Remote Login and add this key to{" "}
          <code>~/.ssh/authorized_keys</code>. Then run{" "}
          <code>ssh user@host true</code> once on the Pi to accept the host key.
          For external volumes, allow full disk access for remote users.
        </p>
        {rsync?.public_key ? (
          <div className="rsync-command-wrap">
            <pre className="rsync-command" aria-label="Pi public key">
              {rsync.public_key}
            </pre>
            <CopyButton
              className="rsync-copy"
              text={rsync.public_key}
              label="Copy key"
            />
          </div>
        ) : (
          <p>
            This Pi has no SSH key yet. Create one as the CameraDeck user with{" "}
            <code>ssh-keygen -t ed25519</code>, then reload this page.
          </p>
        )}
      </details>
    </section>
  );
}

export function Transfer({ capturing }: { capturing: boolean }) {
  const [uploadOpened, setUploadOpened] = useState(false);
  return (
    <>
      <div className="heading">
        <h1>Transfer</h1>
      </div>
      <section className="transfer-section" aria-labelledby="rsync-heading">
        <h2 id="rsync-heading">rsync</h2>
        <p>Run on your Mac after stopping capture.</p>
        <div className="rsync-command-wrap">
          <pre className="rsync-command" tabIndex={0} aria-label="rsync command">
            <code>{RSYNC_COMMAND}</code>
          </pre>
          <CopyButton className="rsync-copy" text={RSYNC_COMMAND} />
        </div>
        <p>
          Copies originals and metadata; skips previews, thumbnails, internal
          files and temporary sidecars. Existing files are skipped, not
          repaired.
        </p>
      </section>
      <PiRsync capturing={capturing} />
      <details
        className="transfer-section"
        onToggle={(e) => {
          if (e.currentTarget.open) setUploadOpened(true);
        }}
      >
        <summary>S3 upload</summary>
        {uploadOpened && <Library capturing={capturing} uploading />}
      </details>
    </>
  );
}
