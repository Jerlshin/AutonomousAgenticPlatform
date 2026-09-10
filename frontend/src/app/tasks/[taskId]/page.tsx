import Link from "next/link";
import { TaskDetail } from "@/components/tasks/TaskDetail";

/**
 * `/tasks/[taskId]` (§8.3): the prompt in full, the task's metadata, and its run history.
 *
 * A Server Component shell. It cannot fetch the task itself — §2.2 forbids reading
 * authenticated REST here with the browser-visible token, and the run history is a live
 * thing anyway — so the frame renders on the server and one client subtree fills it.
 */
export default async function TaskDetailPage({
  params,
}: {
  params: Promise<{ taskId: string }>;
}) {
  const { taskId } = await params;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4">
      <nav aria-label="Breadcrumb" className="text-xs text-muted">
        <Link href="/tasks" className="transition-colors hover:text-fg">
          Tasks
        </Link>
        <span className="mx-1.5 text-idle">/</span>
        <span className="font-mono">{taskId.slice(0, 8)}</span>
      </nav>
      <TaskDetail taskId={taskId} />
    </div>
  );
}
