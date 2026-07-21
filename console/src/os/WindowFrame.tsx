/**
 * WindowFrame.tsx — A single draggable / resizable OS window.
 *
 * Reads geometry from osWindowStore and renders app content passed as
 * children. Dragging uses pointer events on the header; resizing uses a
 * bottom-right handle. Maximise fills the desktop minus the taskbar.
 * On small viewports windows are forced full-screen and drag is disabled.
 */
import { useCallback, useRef, useState } from "react";
import { Minus, X, Maximize2, type LucideIcon } from "lucide-react";
import { useOsWindows, type OsWindow } from "./osWindowStore";
import { computeSnapRect, type SnapZone } from "./snap";
import { OsWindowContainerContext } from "./osWindowContainer";
import { useOsStyles, MENUBAR_H, DOCK_H } from "./useOsStyles";

interface WindowFrameProps {
  win: OsWindow;
  title: string;
  Icon: LucideIcon;
  accent: string;
  isMobile: boolean;
  children: React.ReactNode;
}

const MIN_W = 360;
const MIN_H = 260;

export default function WindowFrame({
  win,
  title,
  Icon,
  accent,
  isMobile,
  children,
}: WindowFrameProps) {
  const { styles, cx } = useOsStyles();
  const {
    focus,
    close,
    minimize,
    toggleMaximize,
    move,
    resize,
    snap,
    activeId,
  } = useOsWindows();
  const dragRef = useRef<{ dx: number; dy: number } | null>(null);
  const resizeRef = useRef<{
    sx: number;
    sy: number;
    w: number;
    h: number;
  } | null>(null);
  // Exposed to descendant overlays (Drawer/Modal) as their render container so
  // they stay within this window instead of covering the whole desktop.
  const [contentEl, setContentEl] = useState<HTMLElement | null>(null);
  // Live edge-snap zone while dragging the header; drives the preview overlay.
  const [snapZone, setSnapZone] = useState<SnapZone | null>(null);
  // Minimize animation: keep the frame mounted briefly to play the transition.
  const [minimizing, setMinimizing] = useState(false);

  const isActive = activeId === win.id;
  const isFull = win.maximized || isMobile;

  const onHeaderPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if ((e.target as HTMLElement).closest("button")) return;
      focus(win.id);
      if (isFull) return;
      dragRef.current = { dx: e.clientX - win.x, dy: e.clientY - win.y };
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
    },
    [focus, isFull, win.id, win.x, win.y],
  );

  const onHeaderPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragRef.current) return;
      const maxX = window.innerWidth - 80;
      const maxY = window.innerHeight - DOCK_H - 40;
      const nx = Math.min(Math.max(0, e.clientX - dragRef.current.dx), maxX);
      const ny = Math.min(
        Math.max(MENUBAR_H, e.clientY - dragRef.current.dy),
        maxY,
      );
      move(win.id, nx, ny);
      const EDGE = 12;
      if (e.clientY <= MENUBAR_H + EDGE) setSnapZone("maximize");
      else if (e.clientX <= EDGE) setSnapZone("left");
      else if (e.clientX >= window.innerWidth - EDGE) setSnapZone("right");
      else setSnapZone(null);
    },
    [move, win.id],
  );

  const endDrag = useCallback(
    (e: React.PointerEvent) => {
      dragRef.current = null;
      if (snapZone) {
        snap(win.id, snapZone);
        setSnapZone(null);
      }
      try {
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* pointer may already be released */
      }
    },
    [snapZone, snap, win.id],
  );

  const onResizePointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      focus(win.id);
      resizeRef.current = { sx: e.clientX, sy: e.clientY, w: win.w, h: win.h };
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
    },
    [focus, win.id, win.w, win.h],
  );

  const onResizePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!resizeRef.current) return;
      const nw = Math.max(
        MIN_W,
        resizeRef.current.w + (e.clientX - resizeRef.current.sx),
      );
      const nh = Math.max(
        MIN_H,
        resizeRef.current.h + (e.clientY - resizeRef.current.sy),
      );
      resize(win.id, { w: nw, h: nh });
    },
    [resize, win.id],
  );

  const endResize = useCallback((e: React.PointerEvent) => {
    resizeRef.current = null;
    try {
      (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  }, []);

  const handleMinimize = useCallback(() => {
    setMinimizing(true);
    window.setTimeout(() => {
      setMinimizing(false);
      minimize(win.id);
    }, 200);
  }, [minimize, win.id]);

  const geometry: React.CSSProperties = isFull
    ? {
        left: 0,
        top: MENUBAR_H,
        width: "100%",
        height: `calc(100% - ${MENUBAR_H}px)`,
        borderRadius: 0,
        zIndex: win.z,
      }
    : {
        left: win.x,
        top: win.y,
        width: win.w,
        height: win.h,
        zIndex: win.z,
      };

  if (win.minimized) return null;

  return (
    <div
      className={cx(
        styles.window,
        isActive && styles.windowActive,
        minimizing && styles.windowMinimizing,
      )}
      style={geometry}
      onPointerDown={() => focus(win.id)}
    >
      {snapZone && <SnapPreview zone={snapZone} />}
      <div
        className={styles.headerMac}
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={endDrag}
        onDoubleClick={() => !isMobile && toggleMaximize(win.id)}
      >
        <div className={styles.lights}>
          <button
            className={cx(styles.light, styles.lightClose)}
            title="Close"
            onClick={() => close(win.id)}
          >
            <X size={8} strokeWidth={3} />
          </button>
          <button
            className={cx(styles.light, styles.lightMin)}
            title="Minimize"
            onClick={handleMinimize}
          >
            <Minus size={8} strokeWidth={3} />
          </button>
          <button
            className={cx(styles.light, styles.lightMax)}
            title="Zoom"
            onClick={() => !isMobile && toggleMaximize(win.id)}
          >
            <Maximize2 size={7} strokeWidth={3} />
          </button>
        </div>
        <div className={styles.macTitle}>
          <Icon size={14} color={accent} />
          {title}
        </div>
        {/* Right spacer keeps the title visually centred. */}
        <div style={{ width: 70 }} />
      </div>

      <div className={styles.content} ref={setContentEl}>
        <OsWindowContainerContext.Provider value={contentEl}>
          {children}
        </OsWindowContainerContext.Provider>
      </div>

      {!isFull && (
        <div
          className={styles.resizeHandle}
          onPointerDown={onResizePointerDown}
          onPointerMove={onResizePointerMove}
          onPointerUp={endResize}
        />
      )}
    </div>
  );
}

function SnapPreview({ zone }: { zone: SnapZone }) {
  const { styles } = useOsStyles();
  const r = computeSnapRect(zone, window.innerWidth, window.innerHeight);
  return (
    <div
      className={styles.snapPreview}
      style={{
        position: "fixed",
        left: r.x,
        top: r.y,
        width: r.w,
        height: r.h,
      }}
    />
  );
}
