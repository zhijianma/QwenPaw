import { arrayMove } from "@dnd-kit/sortable";

/** Reorder stable sidebar entry ids without deriving identity from position. */
export function reorderNavigationEntries(
  entries: string[],
  id: string,
  target: string,
) {
  const from = entries.indexOf(id);
  const to = entries.indexOf(target);
  if (target === "library" || (to === -1 && target !== "preview")) {
    return entries.filter((item) => item !== id);
  }
  if (from === -1) {
    const next = [...entries];
    next.splice(to < 0 ? next.length : to, 0, id);
    return next;
  }
  return arrayMove(entries, from, to < 0 ? entries.length - 1 : to);
}
