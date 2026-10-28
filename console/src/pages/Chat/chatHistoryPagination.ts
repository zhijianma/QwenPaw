const EDGE_THRESHOLD_PX = 96;

export function isAtOlderHistoryEdge(scroller: HTMLElement): boolean {
  const maximum = Math.max(scroller.scrollHeight - scroller.clientHeight, 0);
  return maximum + scroller.scrollTop <= EDGE_THRESHOLD_PX;
}
