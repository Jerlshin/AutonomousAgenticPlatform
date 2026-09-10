import { CodeBrowser } from "@/components/run/CodeBrowser";

/**
 * `/runs/[runId]/code` — the revision diff viewer (§8.7).
 *
 * The same Server/Client split as the deck itself (§2.2): the route resolves the id and
 * renders the client subtree that owns the stream, because the revisions arrive as
 * `code.revision` events and only a Client Component can hold the socket that carries
 * them.
 */
export default async function RunCodePage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <CodeBrowser runId={runId} />;
}
