import { useEffect, useRef, useState, type ReactNode } from "react";
import { Settings2, Camera as CameraIcon } from "lucide-react";
import { request, message } from "./api";
import type { Status } from "./types";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "./components/ui/dialog";

let snapshotPending: Promise<unknown> = Promise.resolve();
function Preview({
  enabled,
  children,
}: {
  enabled: boolean;
  children?: ReactNode;
}) {
  const [src, setSrc] = useState(""),
    [note, setNote] = useState("Waiting for a fresh frame…");
  useEffect(() => {
    let disposed = false,
      url = "",
      timer: ReturnType<typeof setTimeout>,
      controller: AbortController | undefined,
      failures = 0,
      generation = 0;
    const clear = () => {
      controller?.abort();
      clearTimeout(timer);
      if (url) URL.revokeObjectURL(url);
      url = "";
      setSrc("");
    };
    const tick = async () => {
      if (disposed || document.hidden || !enabled) return;
      const current = generation;
      const started = performance.now();
      try {
        await snapshotPending.catch(() => {});
        if (disposed || document.hidden || current !== generation) return;
        const ownController = new AbortController();
        controller = ownController;
        const pending = request("/api/preview", undefined, {
          blob: true,
          signal: ownController.signal,
          timeout: 6000,
        });
        snapshotPending = pending;
        const blob = (await pending) as Blob;
        if (blob.type !== "image/jpeg")
          throw new Error("Preview did not return a JPEG.");
        const next = URL.createObjectURL(blob);
        try {
          const image = new Image();
          image.src = next;
          await image.decode();
        } catch {
          URL.revokeObjectURL(next);
          throw new Error("Could not decode preview.");
        }
        if (
          disposed ||
          document.hidden ||
          ownController.signal.aborted ||
          current !== generation
        ) {
          URL.revokeObjectURL(next);
          return;
        }
        if (url) URL.revokeObjectURL(url);
        url = next;
        setSrc(next);
        setNote("");
        failures = 0;
      } catch (e) {
        if (!disposed && !document.hidden && current === generation) {
          clear();
          setNote(message(e));
          failures++;
        }
      }
      if (!disposed && !document.hidden && current === generation)
        timer = setTimeout(
          tick,
          failures
            ? Math.min(8000, 500 * 2 ** failures)
            : Math.max(0, 125 - (performance.now() - started)),
        );
    };
    const visibility = () => {
      generation++;
      clear();
      setNote(
        document.hidden ? "Preview paused" : "Waiting for a fresh frame…",
      );
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", visibility);
    if (enabled) void tick();
    else setNote("Camera preview unavailable");
    return () => {
      disposed = true;
      clear();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [enabled]);
  return (
    <div className="preview">
      {src ? (
        <img src={src} alt="Latest verified camera frame" />
      ) : (
        <div className="preview-empty">
          <CameraIcon size={36} />
          <p role="status">{note}</p>
          {children}
        </div>
      )}
    </div>
  );
}

export function CameraPage({
  status,
  connected,
  run,
  busy,
}: {
  status: Status | null;
  connected: boolean;
  run: (fn: () => Promise<void>) => void;
  busy: boolean;
}) {
  const [settings, setSettings] = useState(false),
    [rate, setRate] = useState(5);
  const initialized = useRef(false);
  useEffect(() => {
    if (status && !initialized.current) {
      setRate(status.sequence_settings?.burst?.rate ?? 5);
      initialized.current = true;
    }
  }, [status]);
  const active = !!status?.sequence?.active;
  const fresh =
    connected &&
    !!status?.ready &&
    status.metadata_age != null &&
    status.metadata_age <= 5;
  const metadata = status?.metadata ?? {};
  const readings = [
    [
      "Exposure",
      "ExposureTime",
      (v: number) =>
        v < 1000
          ? `${v.toFixed(0)} µs`
          : v < 1_000_000
            ? `${(v / 1000).toFixed(2)} ms`
            : `${(v / 1_000_000).toFixed(2)} s`,
    ],
    ["Analogue gain", "AnalogueGain", (v: number) => `${v.toFixed(2)}×`],
    [
      "Sensor rate",
      "FrameDuration",
      (v: number) => (v > 0 ? `${(1_000_000 / v).toFixed(1)} fps` : "—"),
    ],
    ["Lens position", "LensPosition", (v: number) => `${v.toFixed(2)} dpt`],
    ["White balance", "ColourTemperature", (v: number) => `${v.toFixed(0)} K`],
    [
      "Light level",
      "Lux",
      (v: number) =>
        `${v.toLocaleString("en", { notation: "compact", maximumFractionDigits: 1 })} lx`,
    ],
  ] as const;
  return (
    <section className="camera-page" aria-label="Camera">
      <div className="heading">
        <h1>Camera</h1>
        <Button
          variant="outline"
          onClick={() => setSettings(true)}
          disabled={!status || !connected}
        >
          <Settings2 size={17} /> Settings
        </Button>
      </div>
      <Preview enabled={connected && !!status?.ready}>
        {connected && status && !status.ready && (
          <Button
            className="camera-retry"
            variant="outline"
            disabled={busy}
            onClick={() =>
              run(async () => {
                await request("/api/configure", {});
              })
            }
          >
            Retry camera
          </Button>
        )}
      </Preview>
      <section
        className="camera-readings"
        aria-label="Measured camera statistics"
      >
        <div className="readings-heading">
          <span>Camera readings</span>
          <span className={fresh ? "readings-state fresh" : "readings-state"}>
            {fresh
              ? "Updating live"
              : status?.metadata_age != null
                ? "Readings stale"
                : "Unavailable"}
          </span>
        </div>
        <dl>
          {readings.map(([label, key, format]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>
                {fresh && Number.isFinite(metadata[key])
                  ? format(metadata[key])
                  : "—"}
              </dd>
            </div>
          ))}
        </dl>
      </section>
      <div className="capture-bar">
        <Button
          disabled={busy || !connected || !status?.ready || active}
          onClick={() =>
            run(async () => {
              await request("/api/capture", {});
            })
          }
        >
          <CameraIcon size={18} /> Take photo
        </Button>
        <label className="inline-label">
          <span>Photos / second</span>
          <Input
            aria-label="Photos per second"
            type="number"
            min={0.001}
            max={30}
            step="any"
            value={rate}
            disabled={active}
            onChange={(e) => setRate(Number(e.target.value))}
          />
        </label>
        {!active && (
          <Button
            variant="outline"
            disabled={
              busy ||
              !connected ||
              !status?.ready ||
              !Number.isFinite(rate) ||
              rate <= 0 ||
              rate > 30 ||
              !!status?.transfer_active
            }
            title={
              status?.transfer_active
                ? "Stop the rsync transfer before starting capture."
                : undefined
            }
            onClick={() =>
              run(async () => {
                await request("/api/sequence/start", {
                  mode: "burst",
                  settings: { rate, count: 0 },
                });
              })
            }
          >
            Start
          </Button>
        )}
      </div>
      <Dialog open={settings} onOpenChange={setSettings}>
        <DialogContent>
          <DialogTitle>Camera settings</DialogTitle>
          <DialogDescription>Requested controls</DialogDescription>
          {status && (
            <Settings
              status={status}
              run={run}
              connected={connected}
              disabled={busy || active || !connected}
            />
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}

function Settings({
  status,
  run,
  connected,
  disabled,
}: {
  status: Status;
  run: (fn: () => Promise<void>) => void;
  connected: boolean;
  disabled: boolean;
}) {
  const [values, setValues] = useState<Record<string, number | boolean>>({
    ...status.applied,
  });
  const schema = status.controls;
  const fresh =
    connected && status.metadata_age != null && status.metadata_age <= 5;
  const modernExposure =
    !!schema.ExposureTimeMode && !!schema.AnalogueGainMode;
  const isAuto = (name: string) => {
    const mode = `${name}Mode`;
    return modernExposure
      ? (values[mode] ?? schema[mode].default ?? 0) ===
          (schema[mode].options?.Auto ?? 0)
      : values[name] != null
        ? Number(values[name]) === 0
        : values.AeEnable !== false;
  };
  const apply = (name: string, value: number | boolean) => {
    setValues((old) => ({ ...old, [name]: value }));
    run(async () => {
      await request("/api/controls", { [name]: value });
    });
  };
  const exposure = (name: string, next: boolean) => {
    const controls: Record<string, number | boolean> = {};
    const manualValue = (field: string) => {
      const spec = schema[field];
      const value = Number(
        (!isAuto(field) ? values[field] : undefined) ??
          (fresh ? status.metadata[field] : undefined) ??
          spec.default ??
          spec.min,
      );
      const bounded = Math.max(spec.min, Math.min(spec.max, value));
      return spec.type.startsWith("Integer") ? Math.round(bounded) : bounded;
    };
    if (modernExposure) {
      const mode = `${name}Mode`;
      controls[mode] =
        schema[mode].options?.[next ? "Auto" : "Manual"] ?? (next ? 0 : 1);
      if (!next) controls[name] = manualValue(name);
    } else {
      // Legacy Raspberry Pi controls use zero for auto. AeEnable would reset
      // both controls, so explicitly preserve the other control instead.
      for (const field of ["ExposureTime", "AnalogueGain"])
        if (schema[field])
          controls[field] = (field === name ? next : isAuto(field))
            ? 0
            : manualValue(field);
    }
    run(async () => {
      await request("/api/controls", controls);
      setValues((old) => {
        const updated = { ...old, ...controls };
        if (modernExposure && next) delete updated[name];
        if (!modernExposure) delete updated.AeEnable;
        return updated;
      });
    });
  };
  const field = (name: string, label: string, blocked = false) => {
    const spec = schema[name];
    if (!spec || (spec.size && spec.size > 1)) return null;
    const value = values[name] ?? spec.default ?? spec.min;
    const options = Object.entries(spec.options ?? {}).filter(
      ([, v]) => v >= spec.min && v <= spec.max,
    );
    return (
      <label className="field" key={name}>
        <span>{label}</span>
        {spec.type === "Bool" ? (
          <input
            type="checkbox"
            checked={!!value}
            disabled={disabled || blocked}
            onChange={(e) => apply(name, e.target.checked)}
          />
        ) : options.length ? (
          <select
            value={Number(value)}
            disabled={disabled || blocked}
            onChange={(e) => apply(name, Number(e.target.value))}
          >
            {options.map(([label, v]) => (
              <option key={label} value={v}>
                {label}
              </option>
            ))}
          </select>
        ) : (
          <Input
            type="number"
            min={spec.min}
            max={spec.max}
            step={spec.type === "Float" ? "any" : 1}
            value={
              blocked && ["ExposureTime", "AnalogueGain"].includes(name)
                ? ""
                : Number(value)
            }
            placeholder={
              blocked && ["ExposureTime", "AnalogueGain"].includes(name)
                ? "Auto"
                : undefined
            }
            disabled={disabled || blocked}
            onChange={(e) =>
              setValues((old) => ({ ...old, [name]: Number(e.target.value) }))
            }
            onBlur={(e) => {
              if (e.target.validity.valid && e.target.value !== "")
                apply(name, Number(e.target.value));
            }}
          />
        )}
      </label>
    );
  };
  return (
    <div className="settings-fields">
      {[
        ["ExposureTime", "Exposure time", "Requested exposure · µs"],
        ["AnalogueGain", "Gain", "Requested gain · ×"],
      ].map(([name, label, requested]) =>
        schema[name] ? (
          <div key={name}>
            {(modernExposure || schema.AeEnable) && (
              <label className="field">
                {label} mode
                <select
                  aria-label={`${label} mode`}
                  value={isAuto(name) ? "auto" : "manual"}
                  disabled={disabled}
                  onChange={(e) => exposure(name, e.target.value === "auto")}
                >
                  <option value="auto">Auto</option>
                  <option value="manual">Manual</option>
                </select>
              </label>
            )}
            {field(name, requested, isAuto(name))}
          </div>
        ) : null,
      )}
      <div className="measured">
        Measured exposure:{" "}
        {fresh && status.metadata.ExposureTime != null
          ? `${status.metadata.ExposureTime} µs · ${status.metadata.AnalogueGain ?? "—"}× gain`
          : "Unavailable / stale"}
      </div>
      <label className="field">
        Orientation
        <select
          value={status.rotation}
          disabled={disabled}
          onChange={(e) => {
            const rotation = Number(e.target.value);
            run(async () => {
              await request("/api/configure", { rotation });
            });
          }}
        >
          <option value={0}>0°</option>
          <option value={180}>180°</option>
        </select>
      </label>
      {field("AwbEnable", "Auto white balance")}
      {field("AfMode", "Focus mode")}
      {field(
        "LensPosition",
        "Lens position · dioptres",
        !!schema.AfMode &&
          Number(values.AfMode ?? status.applied.AfMode ?? 0) !== 0,
      )}
      {field("AfRange", "Focus range")}
      {field("AfSpeed", "Focus speed")}
    </div>
  );
}
