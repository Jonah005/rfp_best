"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * A diagram as it was when the assistant answered, shown inside the chat thread.
 *
 * Diagrams have no screen of their own: they are asked for, changed, restyled and
 * reviewed in the Copilot conversation, and the picture appears there. The drawing
 * is an SVG produced by the platform from a checked structure; it is shown through
 * an <img>, which cannot run script whatever the drawing contains.
 */
const picture = (svg: string) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;

/** The drawn diagram as a picture source, for places that lay it out themselves. */
export function useDiagramSrc(diagramId: string, versionNo: number): string | null {
  const shown = useQuery({
    queryKey: ["diagram-version", diagramId, versionNo],
    queryFn: () => api<{ svg: string }>(`/diagrams/${diagramId}/versions/${versionNo}`),
    staleTime: Infinity,
  });
  return shown.data ? picture(shown.data.svg) : null;
}

export function InlineDiagram({ diagramId, versionNo }: { diagramId: string; versionNo: number }) {
  const shown = useQuery({
    queryKey: ["diagram-version", diagramId, versionNo],
    queryFn: () => api<{ svg: string }>(`/diagrams/${diagramId}/versions/${versionNo}`),
    staleTime: Infinity,
  });
  if (shown.isLoading) return <p className="muted-line">Drawing…</p>;
  if (shown.error || !shown.data) return null;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className="diagram-img" alt={`Diagram version ${versionNo}`} src={picture(shown.data.svg)} />;
}
