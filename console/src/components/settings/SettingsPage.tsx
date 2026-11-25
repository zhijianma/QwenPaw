import type { ReactNode } from "react";

import { PageHeader, type PageHeaderProps } from "@/components/PageHeader";

import styles from "./settings.module.less";

export type SettingsPageProps = {
  header: PageHeaderProps;
  navigation?: ReactNode;
  toolbar?: ReactNode;
  feedback?: ReactNode;
  children: ReactNode;
};

/** Shared settings layout that leaves data and interaction state to pages. */
export function SettingsPage({
  header,
  navigation,
  toolbar,
  feedback,
  children,
}: SettingsPageProps) {
  return (
    <div className={styles.page} data-settings-page>
      <PageHeader {...header} />
      {navigation ? (
        <div className={styles.navigation}>{navigation}</div>
      ) : null}
      {toolbar ? <div className={styles.toolbar}>{toolbar}</div> : null}
      {feedback ? <div className={styles.feedback}>{feedback}</div> : null}
      <div className={styles.content}>{children}</div>
    </div>
  );
}
