import { Panel, Skeleton } from "@/components/ui/primitives";

export default function TasksLoading() {
  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      <Panel title="Tasks" className="min-h-0 flex-1 border border-line">
        <Skeleton rows={10} />
      </Panel>
    </div>
  );
}
