"use client";

import { useMemo } from "react";
import { formatMetric } from "@/lib/format";
import type { MetricPoint } from "@/lib/types";

/**
 * One metric's curve, drawn as an inline SVG polyline (§8.5.6).
 *
 * No chart library. A sparkline is a `<polyline>` with a viewBox, and pulling Recharts
 * into the deck for six of them would put ~90 KB in the run view's bundle to draw
 * something a `map` produces (§9.4). The full-size metric chart on the dashboard uses
 * Recharts, dynamically imported, where the axes and tooltips earn it.
 *
 * Two rules from §8.5.6 and §9.3:
 *
 * * **a single-point series renders as a value, not a degenerate one-pixel chart.** One
 *   observation is a number, and drawing it as a line claims a trend that does not exist.
 * * **the data is downsampled before it is drawn.** A `metric.logged` per step over a
 *   long training run is thousands of points inside 96 pixels; past `MAX_POINTS` the
 *   extra vertices are invisible and cost layout on every frame.
 */

/** Beyond this the vertices are sub-pixel. §9.3 caps chart input at ≤ 300 points. */
const MAX_POINTS = 96;
const WIDTH = 96;
const HEIGHT = 20;

export function MetricSparkline({
  metricKey,
  points,
}: {
  metricKey: string;
  points: readonly MetricPoint[];
}) {
  const series = useMemo(
    () => points.filter((point) => point.key === metricKey),
    [points, metricKey],
  );

  const geometry = useMemo(() => {
    if (series.length < 2) return null;
    const sampled = downsample(series, MAX_POINTS);
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    for (const point of sampled) {
      if (point.value < min) min = point.value;
      if (point.value > max) max = point.value;
    }
    const span = max - min || 1;
    const step = WIDTH / (sampled.length - 1);
    const path = sampled
      .map((point, index) => {
        const x = index * step;
        // SVG's y grows downward; a metric that improved must go up.
        const y = HEIGHT - ((point.value - min) / span) * HEIGHT;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    return { path, first: sampled[0]!.value, last: sampled[sampled.length - 1]!.value };
  }, [series]);

  if (series.length === 0) return null;

  const latest = series[series.length - 1]!;

  if (!geometry) {
    // One observation. A value, with its step, and no chart.
    return (
      <span className="inline-flex items-baseline gap-1.5">
        <span className="font-mono text-[11px] text-muted">{metricKey}</span>
        <span className="tnum font-mono text-[11px] text-fg">
          {formatMetric(latest.value)}
        </span>
      </span>
    );
  }

  const improved = geometry.last >= geometry.first;

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="font-mono text-[11px] text-muted">{metricKey}</span>
      <span className="tnum font-mono text-[11px] text-fg">
        {formatMetric(latest.value)}
      </span>
      <svg
        width={WIDTH}
        height={HEIGHT}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`${metricKey} over ${series.length} steps, from ${formatMetric(
          geometry.first,
        )} to ${formatMetric(geometry.last)}`}
        className="shrink-0 overflow-visible"
      >
        <polyline
          points={geometry.path}
          fill="none"
          strokeWidth={1}
          className={improved ? "stroke-[var(--color-ok)]" : "stroke-[var(--color-warn)]"}
        />
      </svg>
    </span>
  );
}

/** Keep the first, the last, and an even spread between them. */
function downsample(points: readonly MetricPoint[], limit: number): MetricPoint[] {
  if (points.length <= limit) return [...points];
  const stride = (points.length - 1) / (limit - 1);
  const out: MetricPoint[] = [];
  for (let index = 0; index < limit; index++) {
    out.push(points[Math.round(index * stride)]!);
  }
  return out;
}
