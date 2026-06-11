import { useCallback, useMemo, useRef, useState } from "react";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface CanvasElement {
  shape: "rect" | "circle" | "ellipse" | "diamond" | "arrow" | "line" | "text" | "image";
  id?: string;
  // rect
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  // circle
  cx?: number;
  cy?: number;
  r?: number;
  // ellipse
  rx?: number;
  ry?: number;
  // diamond
  size?: number;
  // arrow / line
  from?: [number, number];
  to?: [number, number];
  // text
  content?: string;
  fontSize?: number;
  fontWeight?: string;
  // image
  href?: string;
  // shared
  text?: string;
  fill?: string;
  stroke?: string;
  strokeWidth?: number;
  opacity?: number;
  label?: string;
  // arrow style
  dashed?: boolean;
}

interface CanvasBlockProps {
  block: {
    title?: string;
    width?: number;
    height?: number;
    background?: string;
    backgroundImage?: string;
    grid?: boolean;
    elements?: CanvasElement[];
    interactive?: boolean;
  };
}

const GRID_SIZE = 20;

function ArrowHead({ id, color }: { id: string; color: string }) {
  return (
    <marker
      id={id}
      markerWidth="10"
      markerHeight="7"
      refX="9"
      refY="3.5"
      orient="auto"
      markerUnits="strokeWidth"
    >
      <polygon points="0 0, 10 3.5, 0 7" fill={color} />
    </marker>
  );
}

/** Render centered text inside a shape */
function CenteredText({ x, y, fontSize, children }: { x: number; y: number; fontSize?: number; children: string }) {
  return (
    <text
      x={x}
      y={y}
      textAnchor="middle"
      dominantBaseline="central"
      fontSize={fontSize ?? 12}
      fill="#333"
    >
      {children}
    </text>
  );
}

function renderElement(
  el: CanvasElement,
  index: number,
  onClick?: (id: string) => void,
) {
  const cursor = onClick && el.id ? "pointer" : undefined;
  const handleClick = () => {
    if (onClick && el.id) onClick(el.id);
  };
  const strokeW = el.strokeWidth ?? 1.5;
  const fillColor = el.fill ?? "transparent";
  const strokeColor = el.stroke ?? "#333";

  switch (el.shape) {
    case "rect": {
      const x = el.x ?? 0;
      const y = el.y ?? 0;
      const w = el.width ?? 100;
      const h = el.height ?? 60;
      return (
        <g key={index} onClick={handleClick} style={{ cursor }}>
          <rect
            x={x} y={y} width={w} height={h}
            rx={6} ry={6}
            fill={fillColor} stroke={strokeColor} strokeWidth={strokeW} opacity={el.opacity}
          />
          {el.text && <CenteredText x={x + w / 2} y={y + h / 2}>{el.text}</CenteredText>}
        </g>
      );
    }
    case "circle": {
      const cx = el.cx ?? 0;
      const cy = el.cy ?? 0;
      const r = el.r ?? 30;
      return (
        <g key={index} onClick={handleClick} style={{ cursor }}>
          <circle
            cx={cx} cy={cy} r={r}
            fill={fillColor} stroke={strokeColor} strokeWidth={strokeW} opacity={el.opacity}
          />
          {el.text && <CenteredText x={cx} y={cy}>{el.text}</CenteredText>}
        </g>
      );
    }
    case "ellipse": {
      const cx = el.cx ?? 0;
      const cy = el.cy ?? 0;
      const rx = el.rx ?? 50;
      const ry = el.ry ?? 30;
      return (
        <g key={index} onClick={handleClick} style={{ cursor }}>
          <ellipse
            cx={cx} cy={cy} rx={rx} ry={ry}
            fill={fillColor} stroke={strokeColor} strokeWidth={strokeW} opacity={el.opacity}
          />
          {el.text && <CenteredText x={cx} y={cy}>{el.text}</CenteredText>}
        </g>
      );
    }
    case "diamond": {
      const cx = el.cx ?? 0;
      const cy = el.cy ?? 0;
      const s = (el.size ?? 60) / 2;
      const points = `${cx},${cy - s} ${cx + s},${cy} ${cx},${cy + s} ${cx - s},${cy}`;
      return (
        <g key={index} onClick={handleClick} style={{ cursor }}>
          <polygon
            points={points}
            fill={fillColor} stroke={strokeColor} strokeWidth={strokeW} opacity={el.opacity}
          />
          {el.text && <CenteredText x={cx} y={cy} fontSize={11}>{el.text}</CenteredText>}
        </g>
      );
    }
    case "arrow":
    case "line": {
      const [x1, y1] = el.from ?? [0, 0];
      const [x2, y2] = el.to ?? [100, 100];
      const markerId = `arrow-${index}`;
      const midX = (x1 + x2) / 2;
      const midY = (y1 + y2) / 2;
      return (
        <g key={index}>
          {el.shape === "arrow" && (
            <defs>
              <ArrowHead id={markerId} color={strokeColor} />
            </defs>
          )}
          <line
            x1={x1} y1={y1} x2={x2} y2={y2}
            stroke={strokeColor} strokeWidth={strokeW}
            strokeDasharray={el.dashed ? "6,4" : undefined}
            markerEnd={el.shape === "arrow" ? `url(#${markerId})` : undefined}
            opacity={el.opacity}
          />
          {(el.label || el.text) && (
            <text x={midX} y={midY - 8} textAnchor="middle" fontSize={11} fill="#666">
              {el.label || el.text}
            </text>
          )}
        </g>
      );
    }
    case "text": {
      const x = el.x ?? 0;
      const y = el.y ?? 0;
      return (
        <text
          key={index}
          x={x} y={y}
          fontSize={el.fontSize ?? 14}
          fontWeight={el.fontWeight ?? "normal"}
          fill={el.fill ?? "#333"}
          onClick={handleClick}
          style={{ cursor }}
        >
          {el.content || el.text || ""}
        </text>
      );
    }
    case "image": {
      const x = el.x ?? 0;
      const y = el.y ?? 0;
      const w = el.width ?? 100;
      const h = el.height ?? 100;
      return (
        <image
          key={index}
          href={el.href}
          x={x} y={y} width={w} height={h}
          onClick={handleClick}
          style={{ cursor }}
        />
      );
    }
    default:
      return null;
  }
}

export default function CanvasBlock({ block }: CanvasBlockProps) {
  const submit = useA2UISubmit();
  const svgRef = useRef<SVGSVGElement>(null);

  const canvasW = block.width ?? 800;
  const canvasH = block.height ?? 400;
  const bg = block.background ?? "#fafafa";
  const elements = block.elements ?? [];

  // Unique ID for the grid pattern to avoid SVG id collisions across instances
  const gridPatternId = useMemo(
    () => `canvas-grid-${Math.random().toString(36).slice(2, 8)}`,
    [],
  );

  // Pan & zoom state
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: canvasW, h: canvasH });
  const viewBoxRef = useRef(viewBox);
  viewBoxRef.current = viewBox;

  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, vx: 0, vy: 0 });
  const rafRef = useRef(0);

  const handleWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.1 : 0.9;
      setViewBox((prev) => {
        const newW = prev.w * factor;
        const newH = prev.h * factor;
        const dw = newW - prev.w;
        const dh = newH - prev.h;
        return { x: prev.x - dw / 2, y: prev.y - dh / 2, w: newW, h: newH };
      });
    },
    [],
  );

  // Read from ref to avoid recreating on every viewBox change
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      setIsPanning(true);
      panStart.current = {
        x: e.clientX,
        y: e.clientY,
        vx: viewBoxRef.current.x,
        vy: viewBoxRef.current.y,
      };
    },
    [],
  );

  // RAF-throttled to avoid 60fps setViewBox calls
  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!isPanning || !svgRef.current) return;
      const clientX = e.clientX;
      const clientY = e.clientY;
      cancelAnimationFrame(rafRef.current);
      rafRef.current = requestAnimationFrame(() => {
        if (!svgRef.current) return;
        const rect = svgRef.current.getBoundingClientRect();
        const scaleX = viewBoxRef.current.w / rect.width;
        const scaleY = viewBoxRef.current.h / rect.height;
        const dx = (clientX - panStart.current.x) * scaleX;
        const dy = (clientY - panStart.current.y) * scaleY;
        setViewBox({
          x: panStart.current.vx - dx,
          y: panStart.current.vy - dy,
          w: viewBoxRef.current.w,
          h: viewBoxRef.current.h,
        });
      });
    },
    [isPanning],
  );

  const handleMouseUp = useCallback(() => setIsPanning(false), []);

  const handleClick = useMemo(
    () => (block.interactive ? (id: string) => { submit?.(id); } : undefined),
    [block.interactive, submit],
  );

  return (
    <div className={styles.canvasBlock}>
      {block.title && <div className={styles.canvasTitle}>{block.title}</div>}
      <div className={styles.canvasBody}>
        <svg
          ref={svgRef}
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
          width="100%"
          height={canvasH}
          style={{ background: bg, cursor: isPanning ? "grabbing" : "grab" }}
          onWheel={handleWheel}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
        >
          {block.grid && (
            <>
              <defs>
                <pattern
                  id={gridPatternId}
                  width={GRID_SIZE}
                  height={GRID_SIZE}
                  patternUnits="userSpaceOnUse"
                >
                  <path
                    d={`M ${GRID_SIZE} 0 L 0 0 0 ${GRID_SIZE}`}
                    fill="none"
                    stroke="rgba(0,0,0,0.06)"
                    strokeWidth="0.5"
                  />
                </pattern>
              </defs>
              <rect
                x={viewBox.x}
                y={viewBox.y}
                width={viewBox.w}
                height={viewBox.h}
                fill={`url(#${gridPatternId})`}
              />
            </>
          )}
          {block.backgroundImage && (
            <image
              href={block.backgroundImage}
              x={0} y={0} width={canvasW} height={canvasH}
              preserveAspectRatio="xMidYMid meet"
            />
          )}
          {elements.map((el, i) => renderElement(el, i, handleClick))}
        </svg>
      </div>
      <div className={styles.canvasHint}>Drag to pan / Scroll to zoom</div>
    </div>
  );
}
