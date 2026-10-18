export type Spec = {
  min: number;
  max: number;
  type: string;
  size?: number;
  default?: number | boolean;
  options?: Record<string, number>;
};
export type Status = {
  ready: boolean;
  error?: string;
  rotation: number;
  controls: Record<string, Spec>;
  applied: Record<string, number | boolean>;
  metadata: Record<string, number>;
  metadata_age: number | null;
  frame_age: number | null;
  sequence?: {
    active: boolean;
    completed: number;
    total: number;
    error?: string;
    rate: number | null;
    skipped: number;
    settings?: { rate?: number };
  };
  sequence_settings?: { burst?: { rate: number } };
  transfer_active?: boolean;
  free_bytes?: number;
  total_bytes?: number;
  low_space?: boolean;
  health?: {
    temperature_c: number | null;
    current: string[] | null;
    since_boot: string[] | null;
  };
};
export type RsyncJob = {
  destination: string;
  command: string;
  started: string;
  finished: string | null;
  elapsed: number;
  exit_code: number | null;
  message: string;
  progress: {
    transferred: string;
    percent: number;
    rate: string;
    eta: string;
    files: number | null;
    remaining: number | null;
    total: number | null;
    scanning: boolean;
  } | null;
  log: string[];
};
export type RsyncStatus = {
  state: "idle" | "running" | "stopping" | "succeeded" | "failed" | "cancelled";
  active: boolean;
  destination: string;
  source: string;
  job: RsyncJob | null;
};
export type Media = {
  id: string;
  kind: string;
  created: string;
  file: string;
  width: number;
  height: number;
  bytes: number;
  metadata: Record<string, unknown>;
};
export type MediaPage = {
  items: Media[];
  page: number;
  per_page: number;
  pages: number;
  total: number;
  indexing: boolean;
  indexed: number;
  index_error: string | null;
};
