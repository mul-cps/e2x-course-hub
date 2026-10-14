import { useCallback, useEffect, useRef, useState } from "react";

type State = {
  updatedAt: number | null;
  stale: boolean;
  error: string;
  refreshing: boolean;
  paused: boolean;
};

/** Bounded, cancellable reads; late responses never belong to a new scope. */
export function useReadonlyRefresh<T>(
  scope: string,
  enabled: boolean,
  read: (signal: AbortSignal) => Promise<T>,
  apply: (value: T) => void,
  failed?: (error: unknown) => void,
  automatic = true,
) {
  const [state, setState] = useState<State>({
    updatedAt: null,
    stale: true,
    error: "",
    refreshing: false,
    paused: false,
  });
  const handlers = useRef({ read, apply, failed });
  const reload = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    handlers.current = { read, apply, failed };
  });
  useEffect(() => {
    let disposed = false,
      generation = 0;
    let active: AbortController | undefined;
    setState({
      updatedAt: null,
      stale: true,
      error: "",
      refreshing: false,
      paused: document.hidden,
    });
    async function run(force = false) {
      if (disposed || !enabled || document.hidden || (active && !force)) return;
      active?.abort();
      const controller = new AbortController(),
        ownGeneration = ++generation;
      active = controller;
      const callbacks = handlers.current;
      let timeout: ReturnType<typeof setTimeout> | undefined;
      setState((previous) => ({
        ...previous,
        refreshing: true,
        paused: false,
      }));
      try {
        const value = await Promise.race([
          callbacks.read(controller.signal),
          new Promise<never>((_, reject) => {
            timeout = setTimeout(() => {
              controller.abort();
              reject(new Error("Refresh timed out after 8 seconds."));
            }, 8000);
          }),
        ]);
        if (disposed || generation !== ownGeneration) return;
        callbacks.apply(value);
        setState({
          updatedAt: Date.now(),
          stale: false,
          error: "",
          refreshing: false,
          paused: false,
        });
      } catch (error) {
        if (disposed || generation !== ownGeneration) return;
        callbacks.failed?.(error);
        setState((previous) => ({
          ...previous,
          stale: true,
          error: controller.signal.aborted
            ? "Refresh timed out after 8 seconds."
            : error instanceof Error
              ? error.message
              : String(error),
          refreshing: false,
        }));
      } finally {
        if (timeout !== undefined) clearTimeout(timeout);
        if (active === controller) active = undefined;
      }
    }
    const refresh = () => run(true);
    reload.current = refresh;
    function visibility() {
      if (document.hidden) {
        generation++;
        active?.abort();
        active = undefined;
        setState((previous) => ({
          ...previous,
          stale: true,
          paused: true,
          refreshing: false,
        }));
      } else void run(true);
    }
    if (enabled) void run();
    const interval =
      enabled && automatic ? setInterval(() => void run(), 20000) : undefined;
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      generation++;
      active?.abort();
      if (interval !== undefined) clearInterval(interval);
      document.removeEventListener("visibilitychange", visibility);
      if (reload.current === refresh) reload.current = async () => {};
    };
  }, [scope, enabled, automatic]);
  const refresh = useCallback(() => reload.current(), []);
  return { state, refresh };
}
