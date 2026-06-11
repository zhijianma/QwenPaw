import { useMemo } from "react";
import { toDisplayUrl } from "@/pages/Chat/utils";
import FallbackBlock from "./blocks/FallbackBlock";
import TextBlock from "./blocks/TextBlock";
import DiffBlock from "./blocks/DiffBlock";
import CodeBlock from "./blocks/CodeBlock";
import ImageBlock from "./blocks/ImageBlock";
import TableBlock from "./blocks/TableBlock";
import CardBlock from "./blocks/CardBlock";
import ProgressBlock from "./blocks/ProgressBlock";
import ButtonsBlock from "./blocks/ButtonsBlock";
import FormBlock from "./blocks/FormBlock";
import ChoiceBlock from "./blocks/ChoiceBlock";
import ChartBlock from "./blocks/ChartBlock";
import AudioBlock from "./blocks/AudioBlock";
import VideoBlock from "./blocks/VideoBlock";
import FileBlock from "./blocks/FileBlock";
import DividerBlock from "./blocks/DividerBlock";
import AlertBlock from "./blocks/AlertBlock";
import CollapseBlock from "./blocks/CollapseBlock";
import StatBlock from "./blocks/StatBlock";
import ImageButtonsBlock from "./blocks/ImageButtonsBlock";
import Scene3DBlock from "./blocks/Scene3DBlock";
import MermaidBlock from "./blocks/MermaidBlock";
import CanvasBlock from "./blocks/CanvasBlock";
import DagBlock from "./blocks/DagBlock";
import MindmapBlock from "./blocks/MindmapBlock";
import styles from "./index.module.less";

/* eslint-disable @typescript-eslint/no-explicit-any */
const BLOCK_COMPONENTS: Record<string, React.FC<{ block: any }>> = {
  text: TextBlock,
  diff: DiffBlock,
  code: CodeBlock,
  image: ImageBlock,
  table: TableBlock,
  progress: ProgressBlock,
  buttons: ButtonsBlock,
  form: FormBlock,
  choice: ChoiceBlock,
  chart: ChartBlock,
  audio: AudioBlock,
  video: VideoBlock,
  file: FileBlock,
  divider: DividerBlock,
  alert: AlertBlock,
  collapse: CollapseBlock,
  stat: StatBlock,
  image_buttons: ImageButtonsBlock,
  scene3d: Scene3DBlock,
  mermaid: MermaidBlock,
  canvas: CanvasBlock,
  dag: DagBlock,
  mindmap: MindmapBlock,
};

interface A2UIRendererProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: any;
}

// Marker emitted by the @a2ui_visual Python decorator.
// Must be kept in sync with a2ui_visual.py.
const A2UI_VISUAL_MARKER = "\n<!-- __a2ui_visual__ -->\n";

/**
 * Try to extract a2ui blocks from the tool output text.
 * The @a2ui_visual decorator appends a JSON payload after a marker.
 */
function extractFromOutput(
  content: { type: string; data?: Record<string, unknown> }[] | undefined,
): { blocks?: unknown[]; title?: string } | undefined {
  // content[1] is the tool output in the merged message
  const output = content?.[1]?.data;
  if (!output) return undefined;

  // The output text may be in .output, .text, or .content
  const text =
    (output.output as string) ||
    (output.text as string) ||
    (output.content as string) ||
    "";
  if (typeof text !== "string") return undefined;

  const idx = text.indexOf(A2UI_VISUAL_MARKER);
  if (idx === -1) return undefined;

  const jsonStr = text.slice(idx + A2UI_VISUAL_MARKER.length).trim();
  try {
    const parsed = JSON.parse(jsonStr);
    if (parsed?.__a2ui__) return parsed;
  } catch {
    /* ignore */
  }
  return undefined;
}

export default function A2UIRenderer({ data }: A2UIRendererProps) {
  const { blocks, title } = useMemo(() => {
    const content = data?.content as
      | { type: string; data?: { arguments?: unknown } }[]
      | undefined;

    // --- Source 1: Direct a2ui tool call (blocks in arguments) ---
    const rawArgs = content?.[0]?.data?.arguments;
    let args: { blocks?: unknown[]; title?: string } | undefined;
    if (typeof rawArgs === "string") {
      try {
        args = JSON.parse(rawArgs);
      } catch {
        args = undefined;
      }
    } else if (rawArgs && typeof rawArgs === "object") {
      args = rawArgs as { blocks?: unknown[]; title?: string };
    }

    // --- Source 2: @a2ui_visual decorated tool (blocks in output) ---
    if (!args?.blocks?.length) {
      const fromOutput = extractFromOutput(
        content as { type: string; data?: Record<string, unknown> }[] | undefined,
      );
      if (fromOutput) args = fromOutput;
    }

    // Filter: only keep items that are objects with a string "type" field
    const rawBlocks = args?.blocks ?? [];
    const validBlocks = (Array.isArray(rawBlocks) ? rawBlocks : []).filter(
      (b): b is Record<string, unknown> =>
        b != null && typeof b === "object" && typeof (b as any).type === "string",
    );
    // Resolve media URLs — maps block type to the field containing the URL
    const URL_FIELDS: Record<string, string> = {
      image: "url", audio: "url", video: "url", file: "url", image_buttons: "url",
      card: "image", scene3d: "modelUrl",
    };
    const resolvedBlocks = validBlocks.map((b) => {
      const field = URL_FIELDS[b.type as string];
      if (field && b[field]) {
        return { ...b, [field]: toDisplayUrl(b[field] as string) };
      }
      return b;
    });

    return {
      blocks: resolvedBlocks,
      title: (args?.title ?? "") as string,
    };
  }, [data]);

  if (!blocks.length) return null;

  // Build elements, grouping consecutive card blocks horizontally
  const elements: React.ReactNode[] = [];
  let i = 0;
  while (i < blocks.length) {
    const block = blocks[i];
    const blockType = block.type as string;

    if (blockType === "card") {
      const cardGroup: Record<string, unknown>[] = [];
      while (i < blocks.length && blocks[i].type === "card") {
        cardGroup.push(blocks[i]);
        i++;
      }
      elements.push(
        <div key={`cards-${i}`} className={styles.cardGroup}>
          {cardGroup.map((card, ci) => (
            <CardBlock key={ci} block={card} />
          ))}
        </div>,
      );
    } else {
      const Component = BLOCK_COMPONENTS[blockType] || FallbackBlock;
      elements.push(
        <div key={i} className={styles.blockWrapper}>
          <Component block={block} />
        </div>,
      );
      i++;
    }
  }

  return (
    <div className={styles.a2uiContainer}>
      <div className={styles.a2uiBadge}>
        <svg className={styles.a2uiBadgeIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M3 9h18" />
          <path d="M9 21V9" />
        </svg>
        a2ui
      </div>
      {title && <div className={styles.a2uiTitle}>{title}</div>}
      {elements}
      <div className={styles.a2uiFooter}>— end of a2ui —</div>
    </div>
  );
}
