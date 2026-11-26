import type { ReactNode } from "react";

import InlineHelp from "@/components/InlineHelp";

import styles from "./settings.module.less";

export type SettingsSectionProps = {
  id: string;
  title: ReactNode;
  help?: ReactNode;
  actions?: ReactNode;
  feedback?: ReactNode;
  width?: "readable" | "full";
  children: ReactNode;
  "data-settings-target"?: string;
  "data-settings-labels"?: string;
};

/** Named settings region with explicit search metadata and no form state. */
export function SettingsSection({
  id,
  title,
  help,
  actions,
  feedback,
  width = "full",
  children,
  "data-settings-target": settingsTarget,
  "data-settings-labels": settingsLabels,
}: SettingsSectionProps) {
  const titleId = `${id}-title`;
  return (
    <section
      id={id}
      className={`${styles.section} ${styles[width]}`}
      aria-labelledby={titleId}
      data-setting-block
      data-settings-target={settingsTarget}
      data-settings-labels={settingsLabels}
    >
      <div className={styles.sectionHeader}>
        <div className={styles.sectionTitleGroup}>
          <h2 id={titleId}>{title}</h2>
          {help ? <InlineHelp>{help}</InlineHelp> : null}
        </div>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
      {feedback ? <div className={styles.feedback}>{feedback}</div> : null}
      <div className={styles.sectionContent}>{children}</div>
    </section>
  );
}
