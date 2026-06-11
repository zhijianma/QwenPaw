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
  const [finalSelection, setFinalSelection] = useState<Set<string>>(new Set());
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
      setFinalSelection(new Set([value]));
      setSubmitted(true);
      submit?.(value);
    }
  };

  const handleSubmitMulti = () => {
    if (selected.size === 0) return;
    setFinalSelection(new Set(selected));
    setSubmitted(true);
    submit?.(JSON.stringify(Array.from(selected)));
  };

  if (submitted) {
    const selectedOptions = block.options.filter((opt) =>
      finalSelection.has(opt.value),
    );
    return (
      <div className={styles.choiceBlock}>
        {block.prompt && (
          <div className={styles.choicePrompt}>{block.prompt}</div>
        )}
        {selectedOptions.map((opt) => (
          <div
            key={opt.value}
            className={`${styles.choiceOption} ${styles.choiceSubmitted}`}
          >
            <div className={styles.choiceOptionLabel}>
              <span className={styles.choiceCheckmark}>&#10003;</span>
              {opt.label}
            </div>
            {opt.description && (
              <div className={styles.choiceOptionDesc}>{opt.description}</div>
            )}
          </div>
        ))}
      </div>
    );
  }

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
        >
          <div className={styles.choiceOptionLabel}>{opt.label}</div>
          {opt.description && (
            <div className={styles.choiceOptionDesc}>{opt.description}</div>
          )}
        </div>
      ))}
      {multiSelect && (
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
