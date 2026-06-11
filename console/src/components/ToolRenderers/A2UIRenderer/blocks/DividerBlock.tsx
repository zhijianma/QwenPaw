import { Divider } from "antd";

interface DividerBlockProps {
  block: { text?: string; orientation?: "left" | "center" | "right" };
}

export default function DividerBlock({ block }: DividerBlockProps) {
  return (
    <Divider
      orientation={block.orientation || "center"}
      style={{ margin: "4px 0" }}
    >
      {block.text || null}
    </Divider>
  );
}
