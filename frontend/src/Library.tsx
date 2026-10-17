import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { Download, RefreshCw, Trash2 } from "lucide-react";
import { request, message } from "./api";
import type { Media, MediaPage } from "./types";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "./components/ui/dialog";

const mediaUrl = (item: Media, variant: string) =>
  `/media/${encodeURIComponent(item.id)}/${variant}`;
const pageSteps = [1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500];
const date = (value: string) => new Date(value).toLocaleString();
const positive = (
  value: string | null,
  fallback: number,
  max = Number.MAX_SAFE_INTEGER,
) => {
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? Math.min(n, max) : fallback;
};
export function Library({ capturing }: { capturing: boolean }) {
  const [params, setParams] = useSearchParams();
  const page = positive(params.get("page"), 1),
    perPage = positive(params.get("per_page"), 40, 100),
    kind = ["all", "still", "video"].includes(params.get("kind") ?? "")
      ? params.get("kind")!
      : "all";
  const [data, setData] = useState<MediaPage | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [selected, setSelected] = useState<Set<string>>(new Set()),
    [feedback, setFeedback] = useState(""),
    [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{
    label: string;
    done?: number;
    total?: number;
    detail?: string;
  } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    setElapsed(0);
    if (!progress) return;
    const started = performance.now();
    const timer = setInterval(
      () => setElapsed((performance.now() - started) / 1000),
      1000,
    );
    return () => clearInterval(timer);
  }, [progress?.label]);
  const [viewed, setViewed] = useState<Media | null>(null),
    [dialog, setDialog] = useState<"delete" | null>(null),
    [result, setResult] = useState(""),
    [pageInput, setPageInput] = useState(String(page));
  const [size, setSize] = useState(() => {
    try {
      return ["small", "medium", "large"].includes(
        localStorage.getItem("thumbnail-size") ?? "",
      )
        ? localStorage.getItem("thumbnail-size")!
        : "medium";
    } catch {
      return "medium";
    }
  });
  const anchor = useRef<{ id: string; kind: string } | null>(null),
    pointerSelection = useRef(false),
    operation = useRef<AbortController | null>(null),
    urls = useRef(new Set<string>());
  const scope = `${kind}:${page}:${perPage}`;
  useEffect(() => {
    setPageInput(String(page));
  }, [page]);
  useEffect(() => {
    try {
      localStorage.setItem("thumbnail-size", size);
    } catch {}
  }, [size]);
  useEffect(() => {
    const controller = new AbortController();
    return () => {
      controller.abort();
      operation.current?.abort();
      urls.current.forEach((url) => URL.revokeObjectURL(url));
      urls.current.clear();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true,
      timer: ReturnType<typeof setTimeout>;
    setLoading(true);
    setError("");
    if (anchor.current?.kind !== kind) anchor.current = null;
    const load = async () => {
      try {
        const next: MediaPage = await request(
          `/api/media?kind=${kind}&page=${page}&per_page=${perPage}`,
          undefined,
          { signal: controller.signal },
        );
        if (!alive) return;
        setData(next);
        if (next.page !== page)
          setParams(
            { kind, page: String(next.page), per_page: String(perPage) },
            { replace: true },
          );
        if (next.indexing || capturing)
          timer = setTimeout(load, next.indexing ? 2500 : 5000);
      } catch (e) {
        if (alive) setError(message(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [scope, revision, capturing]);
  const navigate = (target: number, nextKind = kind, nextPerPage = perPage) =>
    setParams({
      kind: nextKind,
      page: String(Math.max(1, target)),
      per_page: String(nextPerPage),
    });
  const items = data?.items ?? [];
  const choose = (id: string, e: MouseEvent) => {
    const next = new Set(selected),
      at = items.findIndex((i) => i.id === id),
      fromAnchor = anchor.current?.kind === kind ? anchor.current : null,
      start = items.findIndex((i) => i.id === fromAnchor?.id),
      removing = e.shiftKey ? e.ctrlKey || e.metaKey : next.has(id);
    const apply = (ids: string[]) => {
      for (const ident of ids) {
        if (removing) next.delete(ident);
        else next.add(ident);
      }
      setSelected(next);
      if (!e.shiftKey || (!fromAnchor && !removing))
        anchor.current = { id, kind };
      setFeedback("");
    };
    if (e.shiftKey && fromAnchor && start < 0) {
      if (removing && !next.size) return;
      void act("Resolving selection", async (signal) => {
        const response = await request(
          "/api/media/range",
          {
            kind,
            start: fromAnchor.id,
            end: id,
            ...(removing ? { ids: [...next] } : {}),
          },
          { signal, timeout: 0 },
        );
        apply(response.ids);
      });
      return;
    }
    if (e.shiftKey) {
      const from = start >= 0 ? start : at;
      apply(
        items
          .slice(Math.min(from, at), Math.max(from, at) + 1)
          .map((item) => item.id),
      );
    } else apply([id]);
  };
  const chooseOnPointerDown = (id: string, e: MouseEvent) => {
    // macOS Control-click can open a context menu without emitting a click.
    // Remember handled gestures even if Control is released before the click.
    pointerSelection.current = e.button === 0 && e.ctrlKey;
    if (pointerSelection.current) {
      e.preventDefault();
      if (!busy) choose(id, e);
    }
  };
  const act = async (
    label: string,
    fn: (signal: AbortSignal) => Promise<void>,
  ) => {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setProgress({ label });
    setFeedback("");
    setResult("");
    try {
      await fn(controller.signal);
    } catch (e) {
      setFeedback(message(e));
      setResult((old) => `${old ? old + "\n" : ""}${message(e)}`);
    } finally {
      operation.current = null;
      setBusy(false);
      setProgress(null);
    }
  };
  const download = () =>
    act("Preparing ZIP", async (signal) => {
      const blob = (await request(
        "/api/media/download",
        { ids: [...selected] },
        {
          blob: true,
          signal,
          timeout: 0,
          onProgress: (done, total) =>
            setProgress({
              label: "Downloading ZIP",
              done,
              total,
              detail: `${(done / 1048576).toFixed(1)}${total ? ` / ${(total / 1048576).toFixed(1)}` : ""} MiB received`,
            }),
        },
      )) as Blob;
      const url = URL.createObjectURL(blob);
      urls.current.add(url);
      const link = document.createElement("a");
      link.href = url;
      link.download = "cameradeck-media.zip";
      link.click();
      setTimeout(() => {
        URL.revokeObjectURL(url);
        urls.current.delete(url);
      }, 60000);
      setFeedback("ZIP download handed to your browser.");
    });
  const remove = () => {
    if (
      !window.confirm(
        `Permanently delete ${selected.size.toLocaleString()} selected captures, including ${offPage.toLocaleString()} on other pages / filters? Originals and metadata will be removed from the Pi. This cannot be undone.`,
      )
    )
      return;
    void act("Deleting captures", async (signal) => {
      const ids = [...selected],
        removed = new Set<string>();
      let deleted = 0,
        missing = 0;
      setProgress({ label: "Deleting captures", done: 0, total: ids.length });
      try {
        // Batches provide progress checkpoints, not a selection cap.
        for (let at = 0; at < ids.length; at += 100) {
          const response = await request(
            "/api/media/delete",
            { ids: ids.slice(at, at + 100) },
            { signal, timeout: 0 },
          );
          for (const id of [...response.deleted, ...response.missing])
            removed.add(id);
          deleted += response.deleted.length;
          missing += response.missing.length;
          setProgress({
            label: "Deleting captures",
            done: Math.min(at + 100, ids.length),
            total: ids.length,
          });
        }
      } finally {
        setSelected(
          (old) => new Set([...old].filter((id) => !removed.has(id))),
        );
        if (anchor.current && removed.has(anchor.current.id))
          anchor.current = null;
        setResult(`${deleted} deleted; ${missing} already missing.`);
        setRevision((n) => n + 1);
      }
    });
  };
  const offPage =
    selected.size - items.filter((item) => selected.has(item.id)).length;
  const allItemsOnPageSelected =
    items.length > 0 && items.every((item) => selected.has(item.id));
  const selectPage = () => {
    setSelected((current) => new Set([...current, ...items.map((item) => item.id)]));
    setFeedback("");
  };
  const remaining =
    progress?.total && progress.done && elapsed > 0
      ? Math.ceil((elapsed * (progress.total - progress.done)) / progress.done)
      : null;
  const eta =
    remaining === null
      ? progress?.total
        ? "ETA: estimating…"
        : "ETA unavailable until progress can be measured."
      : `ETA: ${remaining >= 3600 ? `${Math.floor(remaining / 3600)}h ` : ""}${remaining >= 60 ? `${Math.floor((remaining % 3600) / 60)}m ` : ""}${remaining % 60}s remaining (estimated)`;
  const progressBar = progress && (
    <div className="operation-progress" role="status">
      <span>
        {progress.label}
        {progress.detail
          ? ` · ${progress.detail}`
          : progress.total
            ? ` · ${progress.done?.toLocaleString()} / ${progress.total.toLocaleString()} captures processed`
            : "…"}
      </span>
      <progress
        aria-label={progress.label}
        value={progress.total ? progress.done : undefined}
        max={progress.total ?? 1}
      />
      <small>{eta} · Keep this page open until finished.</small>
    </div>
  );
  const pagination = data && !error && (
    <div className="pagination">
      <div
        className="page-jumps previous"
        role="group"
        aria-label="Previous pages"
      >
        {pageSteps.map((step) => (
          <Button
            key={step}
            variant="outline"
            aria-label={step === 1 ? "Previous" : `Previous ${step} pages`}
            title={`Previous ${step} ${step === 1 ? "page" : "pages"}`}
            disabled={loading || page <= 1}
            onClick={() => navigate(page - step)}
          >
            −{step}
          </Button>
        ))}
        <Button
          className="page-edge"
          variant="outline"
          disabled={loading || page <= 1}
          onClick={() => navigate(1)}
        >
          First
        </Button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (Number.isInteger(Number(pageInput)) && Number(pageInput) > 0)
            navigate(Math.min(Number(pageInput), Math.max(1, data.pages)));
        }}
      >
        <label>
          Page{" "}
          <Input
            aria-label="Page number"
            type="number"
            min={1}
            max={Math.max(1, data.pages)}
            value={pageInput}
            onChange={(e) => setPageInput(e.target.value)}
          />
        </label>
        <span>
          of {Math.max(1, data.pages)}
          {data.indexing ? " indexed" : ""}
        </span>
        <Button variant="ghost" disabled={loading}>
          Go
        </Button>
      </form>
      <div className="page-jumps" role="group" aria-label="Next pages">
        {pageSteps.map((step) => (
          <Button
            key={step}
            variant="outline"
            aria-label={step === 1 ? "Next" : `Next ${step} pages`}
            title={`Next ${step} ${step === 1 ? "page" : "pages"}`}
            disabled={loading || page >= data.pages}
            onClick={() => navigate(Math.min(data.pages, page + step))}
          >
            +{step}
          </Button>
        ))}
        <Button
          className="page-edge"
          variant="outline"
          disabled={loading || page >= data.pages}
          onClick={() => navigate(data.pages)}
        >
          Last
        </Button>
      </div>
    </div>
  );
  return (
    <>
      <div className="heading">
        <h1>Library</h1>
        <Button
          variant="outline"
          disabled={loading}
          onClick={() => setRevision((n) => n + 1)}
        >
          <RefreshCw size={16} /> Refresh
        </Button>
      </div>
      <div className="library-controls">
        <label>
          Kind
          <select value={kind} onChange={(e) => navigate(1, e.target.value)}>
            <option value="all">All</option>
            <option value="still">Photos</option>
            <option value="video">Videos</option>
          </select>
        </label>
        <label>
          Thumbnails
          <select value={size} onChange={(e) => setSize(e.target.value)}>
            <option value="small">Small</option>
            <option value="medium">Medium</option>
            <option value="large">Large</option>
          </select>
        </label>
        <label>
          Per page
          <select
            value={perPage}
            onChange={(e) => navigate(1, kind, Number(e.target.value))}
          >
            {[...new Set([20, 40, 80, 100, perPage])]
              .sort((a, b) => a - b)
              .map((n) => (
                <option key={n}>{n}</option>
              ))}
          </select>
        </label>
        <span className="count">
          {data && !loading && !error
            ? data.indexing || data.index_error
              ? `${data.total} indexed captures · total incomplete`
              : `${data.total} captures`
            : "—"}
        </span>
      </div>
      {data?.indexing && (
        <p role="status">
          Indexing… {data.indexed} captures available. Results are incomplete.
        </p>
      )}
      {data?.index_error && (
        <p className="error" role="alert">
          Index error: {data.index_error}. Results may be incomplete.
        </p>
      )}
      <div className="selection-bar">
        <span>
          {selected.size} selected
          {offPage > 0 ? ` · ${offPage} on other pages / filters` : ""}
        </span>
        <Button
          variant="ghost"
          disabled={loading || busy || !items.length || allItemsOnPageSelected}
          onClick={selectPage}
        >
          Select all on page
        </Button>
        <Button
          variant="ghost"
          disabled={!selected.size || busy}
          onClick={() => {
            setSelected(new Set());
            anchor.current = null;
            setFeedback("");
          }}
        >
          Clear
        </Button>
        <Button
          variant="outline"
          disabled={!selected.size || busy}
          onClick={download}
        >
          <Download size={16} /> ZIP
        </Button>
        <Button
          variant="outline"
          disabled={!selected.size || busy}
          onClick={() => {
            setDialog("delete");
            setResult("");
          }}
        >
          <Trash2 size={16} /> Delete
        </Button>
      </div>
      {!dialog && progressBar}
      {feedback && (
        <p role="alert" className="notice">
          {feedback}
        </p>
      )}
      {pagination && <nav aria-label="Top pagination">{pagination}</nav>}
      {loading ? (
        <div className="empty" role="status">
          Loading captures…
        </div>
      ) : error ? (
        <div className="empty error" role="alert">
          {error}
          <Button variant="outline" onClick={() => setRevision((n) => n + 1)}>
            Retry
          </Button>
        </div>
      ) : !items.length ? (
        <div className="empty">
          {data?.indexing
            ? "No captures indexed yet."
            : "No captures on this page."}
        </div>
      ) : (
        <div className={`media-grid ${size}`}>
          {items.map((item) => (
            <article
              key={item.id}
              className={
                selected.has(item.id) ? "media-card selected" : "media-card"
              }
            >
              <button
                className="thumbnail"
                onPointerDown={(e) => chooseOnPointerDown(item.id, e)}
                onContextMenu={(e) => {
                  if (e.ctrlKey) e.preventDefault();
                }}
                onClick={(e) => {
                  if (e.detail > 0 && pointerSelection.current) return;
                  if (e.shiftKey || e.ctrlKey || e.metaKey) {
                    if (!busy) choose(item.id, e);
                  } else setViewed(item);
                }}
                aria-label={`Open ${item.file}`}
              >
                <img
                  loading="lazy"
                  src={mediaUrl(item, "thumb")}
                  alt={item.file}
                />
                {item.kind === "video" && (
                  <span className="video-label">Video</span>
                )}
              </button>
              <label
                className="select-label"
                onPointerDown={(e) => chooseOnPointerDown(item.id, e)}
                onContextMenu={(e) => {
                  if (e.ctrlKey) e.preventDefault();
                }}
                onClick={(e) => {
                  // Use the original label event, not a forwarded checkbox click.
                  if (
                    !(e.target instanceof HTMLInputElement) ||
                    busy ||
                    (e.detail > 0 && pointerSelection.current)
                  )
                    e.preventDefault();
                  if (!busy && !(e.detail > 0 && pointerSelection.current))
                    choose(item.id, e);
                }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(item.id)}
                  aria-label={`Select ${item.file}`}
                  disabled={busy}
                  onChange={() => {}}
                />
                <span>{date(item.created)}</span>
              </label>
              <div className="card-details">
                <span>
                  {item.width} × {item.height}
                </span>
                <a
                  href={mediaUrl(item, "original?download=1")}
                  aria-label={`Download ${item.file}`}
                >
                  <Download size={16} />
                </a>
              </div>
            </article>
          ))}
        </div>
      )}
      {pagination && <nav aria-label="Bottom pagination">{pagination}</nav>}
      <Dialog
        open={!!viewed}
        onOpenChange={(open) => {
          if (!open) setViewed(null);
        }}
      >
        <DialogContent className="viewer">
          <DialogTitle>{viewed?.file}</DialogTitle>
          <DialogDescription>
            {viewed
              ? `${date(viewed.created)} · ${viewed.width} × ${viewed.height} · ${(viewed.bytes / 1048576).toFixed(1)} MB`
              : ""}
          </DialogDescription>
          {viewed && <Viewer item={viewed} />}
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!dialog}
        onOpenChange={(open) => {
          if (!open && !busy) setDialog(null);
        }}
      >
        <DialogContent
          closeDisabled={busy}
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
          }}
          onPointerDownOutside={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <DialogTitle>Delete captures?</DialogTitle>
          <DialogDescription>
            {selected.size} selected captures, including {offPage} on other
            pages / filters.
          </DialogDescription>
          <p>
            Deletion is permanent. Originals and metadata will be removed from
            the Pi.
          </p>
          <Button
            variant="destructive"
            disabled={busy || !selected.size}
            onClick={remove}
          >
            {busy ? "Deleting…" : "Confirm deletion"}
          </Button>
          {progressBar}
          {result && (
            <pre className="result" role="status">
              {result}
            </pre>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
function Viewer({ item }: { item: Media }) {
  const [original, setOriginal] = useState(false),
    [error, setError] = useState("");
  return (
    <>
      {item.kind === "video" ? (
        <video
          controls
          preload="metadata"
          poster={mediaUrl(item, "thumb")}
          src={mediaUrl(item, "original")}
          onError={() =>
            setError("Could not play video. Try downloading the original.")
          }
        />
      ) : (
        <img
          src={mediaUrl(item, original ? "original" : "preview")}
          alt={item.file}
          onError={() =>
            setError("Could not load image. Try downloading the original.")
          }
        />
      )}
      <div className="viewer-actions">
        {item.kind !== "video" && (
          <Button
            variant="outline"
            disabled={original}
            onClick={() => setOriginal(true)}
          >
            Load original
          </Button>
        )}
        <Button variant="outline" asChild>
          <a href={mediaUrl(item, "original?download=1")}>Download original</a>
        </Button>
        <Button variant="ghost" asChild>
          <a href={mediaUrl(item, "metadata?download=1")}>Metadata</a>
        </Button>
      </div>
      {error && <p role="alert">{error}</p>}
    </>
  );
}
