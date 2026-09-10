import { Panel, Skeleton } from "@/components/ui/primitives";

/**
 * The dashboard's loading state (§8.1): the *shape* of the page, never a centred spinner.
 *
 * A skeleton that matches the final layout means nothing moves when the data lands, which
 * is the difference between a page that loads and a page that jumps.
 */
export default function DashboardLoading() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="h-16 border border-line bg-surface" />
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-[2fr_1fr]">
        <Panel title="Active & queued" className="border border-line">
          <Skeleton rows={3} />
        </Panel>
        <Panel title="Dependencies" className="border border-line">
          <Skeleton rows={3} />
        </Panel>
      </div>
      <Panel title="Recent runs" className="border border-line">
        <Skeleton rows={6} />
      </Panel>
    </div>
  );
}
