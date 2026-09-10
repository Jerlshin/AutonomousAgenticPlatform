import { RunDeck } from "@/components/run/RunDeck";

/**
 * `/runs/[runId]` — the live run control deck (§8.5).
 *
 * A Server Component that renders nothing but the client subtree that owns the stream.
 * The canonical shape from §2.2: a Server Component cannot hold a socket, and a Client
 * Component wrapping the whole page would turn every `token.delta` into a full-tree
 * reconciliation.
 */
export default async function RunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <RunDeck runId={runId} />;
}
