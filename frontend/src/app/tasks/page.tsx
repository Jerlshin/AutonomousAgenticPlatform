import { TaskTable } from "@/components/tasks/TaskTable";

/** `/tasks` (§8.3): a Server shell around one client table. */
export default function TasksPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      <TaskTable />
    </div>
  );
}
