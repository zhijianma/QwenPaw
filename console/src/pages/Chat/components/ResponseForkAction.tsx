import { useState } from "react";
import { Tooltip } from "antd";
import { IconButton } from "@agentscope-ai/design";
import { GitBranch } from "lucide-react";
import { useTranslation } from "react-i18next";

export function ResponseForkAction({
  data,
  disabled,
  onFork,
}: {
  data: { chat_turn_id?: string; status?: string };
  disabled: boolean;
  onFork: (turnId: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [pending, setPending] = useState(false);
  if (
    !data.chat_turn_id ||
    !["completed", "canceled", "cancelled", "failed"].includes(
      data.status ?? "",
    )
  )
    return null;

  return (
    <Tooltip title={t("chat.fork.action")}>
      <IconButton
        aria-label={t("chat.fork.action")}
        icon={<GitBranch size={16} />}
        bordered={false}
        disabled={disabled || pending}
        onClick={async () => {
          if (pending) return;
          setPending(true);
          try {
            await onFork(data.chat_turn_id!);
          } finally {
            setPending(false);
          }
        }}
      />
    </Tooltip>
  );
}
