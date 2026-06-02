import { useState } from "react";
import { Button } from "antd";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

interface ButtonDef {
  label: string;
  value: string;
  style?: "primary" | "default" | "danger";
}

interface ButtonsBlockProps {
  block: { buttons?: ButtonDef[] };
}

export default function ButtonsBlock({ block }: ButtonsBlockProps) {
  const [clicked, setClicked] = useState<string | null>(null);
  const submit = useA2UISubmit();

  if (!block.buttons?.length) return null;

  const handleClick = (btn: ButtonDef) => {
    setClicked(btn.value);
    submit?.(btn.value);
  };

  return (
    <div className={styles.buttonsBlock}>
      {block.buttons.map((btn, i) => (
        <Button
          key={i}
          type={
            btn.style === "danger"
              ? "primary"
              : btn.style === "default"
                ? "default"
                : "primary"
          }
          danger={btn.style === "danger"}
          disabled={clicked !== null}
          onClick={() => handleClick(btn)}
        >
          {btn.label}
        </Button>
      ))}
    </div>
  );
}
