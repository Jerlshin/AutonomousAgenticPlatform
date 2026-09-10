/**
 * A `DEBUG`-gated logger.
 *
 * docs/FRONTEND.md §9.3: "`console.log` in a hot path is a performance bug." The console
 * pane's fold runs per event and the virtualizer's row renderer runs per visible row per
 * frame; a `console.log` in either costs more than the work it is reporting on, and in
 * Chrome a retained console object also keeps the whole event alive.
 *
 * Gating on an environment variable rather than `NODE_ENV` is deliberate: the interesting
 * case is a production build misbehaving on someone's machine, and `NEXT_PUBLIC_DEBUG_STREAM=1`
 * turns the instrumentation on there without a rebuild.
 */

export const DEBUG_STREAM = process.env.NEXT_PUBLIC_DEBUG_STREAM === "1";

type Args = readonly unknown[];

export const log = {
  stream(...args: Args): void {
    if (DEBUG_STREAM) console.debug("[stream]", ...args);
  },
  store(...args: Args): void {
    if (DEBUG_STREAM) console.debug("[store]", ...args);
  },
  /** Always emitted. A warning the operator may need to act on is not debug output. */
  warn(...args: Args): void {
    console.warn("[pluton]", ...args);
  },
};
