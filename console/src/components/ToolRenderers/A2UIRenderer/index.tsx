import { useMemo } from "react";
import type {
  IAgentScopeRuntimeMessage,
  IDataContent,
} from "@agentscope-ai/chat";
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
  data: IAgentScopeRuntimeMessage;
}

export default function A2UIRenderer({ data }: A2UIRendererProps) {
  const { blocks, title } = useMemo(() => {
    const content = data.content as IDataContent<{
      name: string;
      arguments: { blocks?: unknown[]; title?: string };
    }>[];
    const args = content?.[0]?.data?.arguments;
    return {
      blocks: (args?.blocks ?? []) as Record<string, unknown>[],
      title: (args?.title ?? "") as string,
    };
  }, [data]);

  if (!blocks.length) return null;

  // Build elements, grouping consecutive card blocks horizontally
  const elements: React.ReactNode[] = [];
  let i = 0;
  while (i < blocks.length) {
    const block = blocks[i];
    const blockType = String(block.type || "");

    if (blockType === "card") {
      const cardGroup: Record<string, unknown>[] = [];
      while (i < blocks.length && String(blocks[i].type) === "card") {
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
      {title && <div className={styles.a2uiTitle}>{title}</div>}
      {elements}
    </div>
  );
}
