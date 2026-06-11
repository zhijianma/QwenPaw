import { useState } from "react";
import { Button, Tooltip } from "antd";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

interface OverlayButton {
  label: string;
  value: string;
  /** Position as percentage from top-left: [x%, y%] */
  position?: [number, number];
  style?: "primary" | "default" | "danger";
  tooltip?: string;
}

interface ImageButtonsBlockProps {
  block: {
    url?: string;
    alt?: string;
    width?: number;
    buttons?: OverlayButton[];
  };
}

export default function ImageButtonsBlock({ block }: ImageButtonsBlockProps) {
  const [clicked, setClicked] = useState<string | null>(null);
  const submit = useA2UISubmit();

  if (!block.url) return null;

  const handleClick = (btn: OverlayButton) => {
    setClicked(btn.value);
    submit?.(btn.value);
  };

  const buttons = block.buttons || [];

  return (
    <div
      className={styles.imageButtonsBlock}
      style={block.width ? { maxWidth: block.width } : undefined}
    >
      <img
        src={block.url}
        alt={block.alt || ""}
        className={styles.imageButtonsImg}
      />
      <div className={styles.imageButtonsOverlay}>
        {buttons.map((btn, i) => {
          const btnStyle: React.CSSProperties = btn.position
            ? { position: "absolute", left: `${btn.position[0]}%`, top: `${btn.position[1]}%` }
            : {};

          const buttonEl = (
            <Button
              key={i}
              type={btn.style === "default" ? "default" : "primary"}
              danger={btn.style === "danger"}
              size="small"
              disabled={clicked !== null}
              onClick={() => handleClick(btn)}
              className={styles.imageOverlayBtn}
              style={btnStyle}
            >
              {btn.label}
            </Button>
          );

          return btn.tooltip ? (
            <Tooltip key={i} title={btn.tooltip}>
              {buttonEl}
            </Tooltip>
          ) : (
            buttonEl
          );
        })}
      </div>
    </div>
  );
}
