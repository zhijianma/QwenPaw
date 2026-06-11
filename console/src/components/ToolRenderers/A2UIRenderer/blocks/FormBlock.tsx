import { useState, useRef, type KeyboardEvent } from "react";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface FieldDef {
  name: string;
  label: string;
  field_type: string;
  required?: boolean;
  placeholder?: string;
  options?: string[];
  min?: number;
  max?: number;
}

interface FormBlockProps {
  block: {
    title?: string;
    fields?: FieldDef[];
    submit_label?: string;
    skip_label?: string;
    result_header?: string;
  };
}

function formatValue(value: unknown, field: FieldDef): string {
  if (value == null || value === "") return "-";
  if (field.field_type === "checkbox") return value ? "Yes" : "No";
  return String(value);
}

export default function FormBlock({ block }: FormBlockProps) {
  const submit = useA2UISubmit();
  const fields = block.fields ?? [];
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of fields) init[f.name] = "";
    return init;
  });
  const [submitted, setSubmitted] = useState(false);
  const [skipped, setSkipped] = useState(false);
  const inputRefs = useRef<(HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null)[]>([]);

  if (!fields.length) return null;

  const handleChange = (name: string, val: string) => {
    setValues((prev) => ({ ...prev, [name]: val }));
  };

  const handleSubmit = () => {
    setSubmitted(true);
    submit?.(JSON.stringify(values));
  };

  const handleSkip = () => {
    setSkipped(true);
    submit?.(JSON.stringify({ __skipped__: true }));
  };

  const handleKeyDown = (e: KeyboardEvent, idx: number) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (idx < fields.length - 1) {
        inputRefs.current[idx + 1]?.focus();
      } else {
        handleSubmit();
      }
    }
  };

  // Skipped
  if (skipped) {
    return (
      <div className={styles.formCard}>
        <div className={styles.formSkipped}>
          {block.title || "Form"} — skipped
        </div>
      </div>
    );
  }

  // Submitted — Q&A summary
  if (submitted) {
    return (
      <div className={styles.formCard}>
        <div className={styles.formResultHeader}>
          {block.result_header ?? "User provided the following:"}
        </div>
        <div className={styles.formResultCard}>
          {fields.map((field) => (
            <div key={field.name} className={styles.formResultItem}>
              <div className={styles.formResultLabel}>{field.label}</div>
              <div className={styles.formResultValue}>
                {formatValue(values[field.name], field)}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // Active form
  return (
    <div className={styles.formCard}>
      <div className={styles.formCardInner}>
        {block.title && (
          <div className={styles.formCardTitle}>{block.title}</div>
        )}

        {fields.map((field, idx) => (
          <div key={field.name} className={styles.formFieldGroup}>
            <div className={styles.formFieldLabel}>
              {fields.length > 1 ? `${idx + 1}. ${field.label}` : field.label}
            </div>
            {field.field_type === "textarea" ? (
              <textarea
                ref={(el) => { inputRefs.current[idx] = el; }}
                className={styles.formTextarea}
                placeholder={field.placeholder}
                value={values[field.name] ?? ""}
                onChange={(e) => handleChange(field.name, e.target.value)}
                rows={2}
              />
            ) : field.field_type === "select" ? (
              <select
                ref={(el) => { inputRefs.current[idx] = el; }}
                className={styles.formInput}
                value={values[field.name] ?? ""}
                onChange={(e) => handleChange(field.name, e.target.value)}
              >
                <option value="">{field.placeholder || "Please select..."}</option>
                {field.options?.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input
                ref={(el) => { inputRefs.current[idx] = el; }}
                className={styles.formInput}
                type={field.field_type === "number" ? "number" : "text"}
                placeholder={field.placeholder}
                value={values[field.name] ?? ""}
                onChange={(e) => handleChange(field.name, e.target.value)}
                onKeyDown={(e) => handleKeyDown(e, idx)}
                min={field.min}
                max={field.max}
              />
            )}
          </div>
        ))}

        <div className={styles.formActions}>
          {block.skip_label !== undefined && (
            <button
              className={styles.formBtnSkip}
              onClick={handleSkip}
              type="button"
            >
              {block.skip_label || "Skip"}
            </button>
          )}
          <button
            className={styles.formBtnSubmit}
            onClick={handleSubmit}
            type="button"
          >
            {block.submit_label || "Submit"}
          </button>
        </div>
      </div>
    </div>
  );
}
