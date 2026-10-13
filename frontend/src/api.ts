let unauthorized = () => {};
export function onUnauthorized(callback: () => void) {
  unauthorized = callback;
}
export async function request(
  path: string,
  body?: unknown,
  options: { signal?: AbortSignal; timeout?: number; blob?: boolean } = {},
) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(abort, options.timeout ?? 15000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    if (response.status === 401 && path !== "/api/login") {
      unauthorized();
      throw new Error("Session locked. Sign in again.");
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return options.blob ? await response.blob() : await response.json();
  } catch (error) {
    if (controller.signal.aborted)
      throw new Error(
        options.signal?.aborted
          ? "Request cancelled."
          : "Request timed out. The operation may still be running on the Pi.",
      );
    if (error instanceof TypeError)
      throw new Error(
        "Cannot reach the Pi. Check your connection; operations may still be running.",
      );
    throw error;
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}
export const message = (e: unknown) =>
  e instanceof Error ? e.message : String(e);
