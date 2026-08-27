export function countDiarioValueItems(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

export function countDiarioRegistri(
  body: Record<string, unknown>,
  hasNarrative: boolean,
): number {
  return (
    (hasNarrative ? 1 : 0) +
    countDiarioValueItems(body.what_changed) +
    countDiarioValueItems(body.decisions_observed) +
    countDiarioValueItems(body.open_loops) +
    countDiarioValueItems(body.notable_context) +
    countDiarioValueItems(body.tomorrow_watch) +
    countDiarioValueItems(body.sources)
  );
}
