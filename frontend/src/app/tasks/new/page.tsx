import Link from "next/link";
import { TaskForm } from "@/components/tasks/TaskForm";

/** `/tasks/new` (§8.4). */
export default function NewTaskPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-auto p-4">
      <nav aria-label="Breadcrumb" className="mb-3 text-xs text-muted">
        <Link href="/tasks" className="transition-colors hover:text-fg">
          Tasks
        </Link>
        <span className="mx-1.5 text-idle">/</span>
        <span>New</span>
      </nav>
      <div className="mx-auto w-full max-w-3xl">
        <TaskForm />
      </div>
    </div>
  );
}
