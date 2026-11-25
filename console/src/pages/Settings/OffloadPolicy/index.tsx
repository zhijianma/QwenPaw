import { useTranslation } from "react-i18next";
import { SettingsPage } from "@/components/settings";
import styles from "./index.module.less";
import { OffloadPolicyCard } from "./OffloadPolicyCard";

export default function OffloadPolicyPage() {
  const { t } = useTranslation();

  return (
    <SettingsPage header={{ current: t("nav.offloadPolicy", "Tool Offload") }}>
      <div className={styles.page}>
        <OffloadPolicyCard />
      </div>
    </SettingsPage>
  );
}
