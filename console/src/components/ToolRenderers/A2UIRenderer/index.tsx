import { useMemo } from "react";
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
};

interface A2UIRendererProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: any;
}

export default function A2UIRenderer({ data }: A2UIRendererProps) {
  const { blocks, title } = useMemo(() => {
    const content = data?.content as
      | { type: string; data?: { arguments?: unknown } }[]
      | undefined;
    const rawArgs = content?.[0]?.data?.arguments;
    // arguments may be a JSON string or already-parsed object
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
    // Filter: only keep items that are objects with a string "type" field
    const rawBlocks = args?.blocks ?? [];
    const validBlocks = (Array.isArray(rawBlocks) ? rawBlocks : []).filter(
      (b): b is Record<string, unknown> =>
        b != null && typeof b === "object" && typeof (b as any).type === "string",
    );
    return {
      blocks: validBlocks,
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
