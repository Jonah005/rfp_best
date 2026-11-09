import { notFound } from "next/navigation";
import { Workspace } from "@/components/workspace";
import { screens, type ScreenKey } from "@/lib/screens";

/**
 * The ten screens are a closed, known set, so they can be emitted as static
 * pages. That is what lets the single-process local runner serve the UI from
 * FastAPI with no Node process at runtime.
 */
export function generateStaticParams() {
  return Object.keys(screens).map((screen) => ({ screen }));
}

export default async function ScreenPage({ params }: { params: Promise<{ screen: string }> }) {
  const { screen } = await params;
  if (!(screen in screens)) notFound();
  return <Workspace screen={screen as ScreenKey} />;
}
