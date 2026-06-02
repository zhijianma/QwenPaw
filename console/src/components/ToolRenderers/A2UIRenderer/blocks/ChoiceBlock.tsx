import { useState } from "react";
import { Button } from "antd";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

interface ChoiceOption {
  label: string;
  value: string;
  description?: string;
}

interface ChoiceBlockProps {
  block: {
    prompt?: string;
    options?: ChoiceOption[];
    multi_select?: boolean;
  };
}

export default function ChoiceBlock({ block }: ChoiceBlockProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitted, setSubmitted] = useState(false);
  const submit = useA2UISubmit();

  if (!block.options?.length) return null;

  const multiSelect = block.multi_select ?? false;

  const handleSelect = (value: string) => {
    if (submitted) return;

    if (multiSelect) {
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(value)) next.delete(value);
        else next.add(value);
        return next;
      });
    } else {
      setSubmitted(true);
      submit?.(value);
    }
  };

  const handleSubmitMulti = () => {
    if (selected.size === 0) return;
    setSubmitted(true);
    submit?.(JSON.stringify(Array.from(selected)));
  };

  return (
    <div className={styles.choiceBlock}>
      {block.prompt && (
        <div className={styles.choicePrompt}>{block.prompt}</div>
      )}
      {block.options.map((opt) => (
        <div
          key={opt.value}
          className={`${styles.choiceOption} ${selected.has(opt.value) ? styles.selected : ""}`}
          onClick={() => handleSelect(opt.value)}
          style={{
            opacity: submitted ? 0.6 : 1,
            pointerEvents: submitted ? "none" : "auto",
          }}
        >
          <div className={styles.choiceOptionLabel}>{opt.label}</div>
          {opt.description && (
            <div className={styles.choiceOptionDesc}>{opt.description}</div>
          )}
        </div>
      ))}
      {multiSelect && !submitted && (
        <Button
          type="primary"
          disabled={selected.size === 0}
          onClick={handleSubmitMulti}
          style={{ alignSelf: "flex-start" }}
        >
          Confirm ({selected.size})
        </Button>
      )}
    </div>
  );
}
