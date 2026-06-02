import { useState } from "react";
import {
  Button,
  Checkbox,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Select,
} from "antd";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";

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
    fields?: FieldDef[];
    submit_label?: string;
  };
}

export default function FormBlock({ block }: FormBlockProps) {
  const [form] = Form.useForm();
  const [submitted, setSubmitted] = useState(false);
  const submit = useA2UISubmit();

  if (!block.fields?.length) return null;

  const handleFinish = (values: Record<string, unknown>) => {
    setSubmitted(true);
    submit?.(JSON.stringify(values));
  };

  const renderField = (field: FieldDef) => {
    switch (field.field_type) {
      case "textarea":
        return <Input.TextArea placeholder={field.placeholder} rows={3} />;
      case "select":
        return (
          <Select placeholder={field.placeholder}>
            {field.options?.map((opt) => (
              <Select.Option key={opt} value={opt}>
                {opt}
              </Select.Option>
            ))}
          </Select>
        );
      case "number":
        return (
          <InputNumber
            placeholder={field.placeholder}
            min={field.min}
            max={field.max}
            style={{ width: "100%" }}
          />
        );
      case "date":
        return <DatePicker style={{ width: "100%" }} />;
      case "checkbox":
        return <Checkbox>{field.label}</Checkbox>;
      default:
        return <Input placeholder={field.placeholder} />;
    }
  };

  return (
    <div className={styles.formBlock}>
      <Form
        form={form}
        layout="vertical"
        onFinish={handleFinish}
        disabled={submitted}
        size="small"
      >
        {block.fields.map((field) => (
          <Form.Item
            key={field.name}
            name={field.name}
            label={field.field_type === "checkbox" ? undefined : field.label}
            rules={
              field.required
                ? [{ required: true, message: `${field.label} is required` }]
                : undefined
            }
            valuePropName={
              field.field_type === "checkbox" ? "checked" : undefined
            }
          >
            {renderField(field)}
          </Form.Item>
        ))}
        <Form.Item>
          <Button type="primary" htmlType="submit" disabled={submitted}>
            {block.submit_label || "Submit"}
          </Button>
        </Form.Item>
      </Form>
    </div>
  );
}
