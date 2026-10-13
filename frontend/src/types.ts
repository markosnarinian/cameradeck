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
