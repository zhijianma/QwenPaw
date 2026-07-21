/**
 * osWindowContainer.tsx — Scopes antd overlays (Drawer / Modal) to the app
 * window instead of document.body when running inside the Desktop OS.
 *
 * WindowFrame provides its scrollable content element via this context. Page
 * components call useOverlayContainer() and pass the result to a Drawer/Modal
 * `getContainer` prop. Outside the OS (classic MainLayout) there is no
 * provider, so the hook returns undefined and antd keeps its default behaviour
 * (rendering to document.body) — fully backward compatible.
 */
import { createContext, useContext } from "react";

/** The OS window's content DOM node, or null when not yet mounted. */
export const OsWindowContainerContext = createContext<HTMLElement | null>(null);

/**
 * Returns the enclosing OS window content element to use as an overlay
 * container, or undefined when not inside an OS window (classic layout).
 */
export function useOverlayContainer(): HTMLElement | undefined {
  return useContext(OsWindowContainerContext) ?? undefined;
}
