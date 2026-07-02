import { useState, useEffect, useCallback } from "react";
import { Modal, InputNumber, Form, Collapse, Input, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { providerApi } from "../../../api/modules/provider";
import type { ModelInfo, ProviderInfo } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";

interface ModelParamsModalProps {
  open: boolean;
  providerId: string;
  model: ModelInfo | null;
  onClose: () => void;
  onSaved: (updatedProvider: ProviderInfo) => void;
}

export function ModelParamsModal({
  open,
  providerId,
  model,
  onClose,
  onSaved,
}: ModelParamsModalProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);

  const [maxTokens, setMaxTokens] = useState<number>(8192);
  const [maxInputLength, setMaxInputLength] = useState<number>(131072);
  const [kwargsText, setKwargsText] = useState("{}");

  useEffect(() => {
    if (!open || !model) return;
    setLoading(true);

    setMaxTokens(model.max_tokens || 8192);
    setMaxInputLength(model.max_input_length || 131072);

    const kwargs = model.generate_kwargs || {};
    setKwargsText(
      Object.keys(kwargs).length > 0 ? JSON.stringify(kwargs, null, 2) : "{}",
    );
    setLoading(false);
  }, [open, model]);

  const handleSave = useCallback(async () => {
    if (!model) return;
    setSaving(true);
    try {
      let parsedKwargs: Record<string, unknown> = {};
      try {
        parsedKwargs = JSON.parse(kwargsText);
      } catch {
        message.error(
          t("modelSelector.params.invalidJson", "Invalid JSON format"),
        );
        setSaving(false);
        return;
      }

      const updatedProvider = await providerApi.configureModel(
        providerId,
        model.id,
        {
          max_tokens: maxTokens,
          max_input_length: maxInputLength,
          generate_kwargs: parsedKwargs,
        },
      );

      message.success(
        t("modelSelector.params.saved", "Model parameters saved"),
      );

      window.dispatchEvent(
        new CustomEvent("model-switched", {
          detail: { maxInputLength },
        }),
      );

      onSaved(updatedProvider);
      onClose();
    } catch {
      message.error(
        t("modelSelector.params.saveFailed", "Failed to save parameters"),
      );
    } finally {
      setSaving(false);
    }
  }, [
    model,
    providerId,
    maxTokens,
    maxInputLength,
    kwargsText,
    message,
    t,
    onSaved,
    onClose,
  ]);

  return (
    <Modal
      open={open}
      title={
        model
          ? t("modelSelector.params.title", {
              model: model.name || model.id,
              defaultValue: `${model.name || model.id} Parameters`,
            })
          : ""
      }
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving}
      okText={t("common.save", "Save")}
      cancelText={t("common.cancel", "Cancel")}
      width={480}
      destroyOnClose
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 32 }}>
          <Spin />
        </div>
      ) : (
        <Form layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            label={t("modelSelector.params.maxTokens", "Max Tokens")}
            tooltip={t(
              "modelSelector.params.maxTokensTooltip",
              "Maximum number of tokens the model can generate in a single response.",
            )}
          >
            <InputNumber
              min={1}
              step={1024}
              value={maxTokens}
              onChange={(v) => v !== null && setMaxTokens(v)}
              style={{ width: "100%" }}
            />
          </Form.Item>

          <Form.Item
            label={t(
              "modelSelector.params.maxInputLength",
              "Max Context Length",
            )}
            tooltip={t(
              "modelSelector.params.maxInputLengthTooltip",
              "Maximum number of input tokens the model context window supports.",
            )}
          >
            <InputNumber
              min={1000}
              step={1024}
              value={maxInputLength}
              onChange={(v) => v !== null && setMaxInputLength(v)}
              style={{ width: "100%" }}
            />
          </Form.Item>

          <Collapse
            ghost
            size="small"
            items={[
              {
                key: "advanced",
                label: t(
                  "modelSelector.params.advanced",
                  "Advanced (generate_kwargs)",
                ),
                children: (
                  <Input.TextArea
                    value={kwargsText}
                    onChange={(e) => setKwargsText(e.target.value)}
                    rows={6}
                    spellCheck={false}
                    style={{
                      fontFamily: "monospace",
                      fontSize: 12,
                    }}
                    placeholder='{ "temperature": 0.7, "top_p": 0.95 }'
                  />
                ),
              },
            ]}
          />
        </Form>
      )}
    </Modal>
  );
}
