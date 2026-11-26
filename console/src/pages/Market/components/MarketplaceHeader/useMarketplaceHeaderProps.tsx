import type { ReactNode } from "react";
import { Tabs, type TabsProps } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import type { PageHeaderProps } from "@/components/PageHeader";

import styles from "./index.module.less";

export type MarketplaceSection = "apps" | "plugins" | "skills" | "community";

const SECTION_PATHS: Record<MarketplaceSection, string> = {
  community: "/market?tab=community",
  apps: "/market",
  plugins: "/market?tab=plugins",
  skills: "/market?tab=skills",
};

export interface MarketplaceHeaderProps {
  activeSection: MarketplaceSection;
  extra?: ReactNode;
}

/** Build the shared marketplace header for direct or settings layouts. */
export function useMarketplaceHeaderProps({
  activeSection,
  extra,
}: MarketplaceHeaderProps): PageHeaderProps {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const items: TabsProps["items"] = [
    { key: "apps", label: t("nav.apps", "Apps") },
    { key: "plugins", label: t("nav.plugins", "Plugins") },
    { key: "skills", label: t("nav.skills", "Skills") },
    { key: "community", label: t("communityCompose.nav", "Community") },
  ];

  const handleChange = (section: string) => {
    const path = SECTION_PATHS[section as MarketplaceSection].replace(
      "/market",
      pathname.startsWith("/settings/") ? "/settings/marketplace" : "/market",
    );
    const target = searchParams.get("target");
    const targetSuffix =
      target === "pool" || target === "workspace"
        ? `${path.includes("?") ? "&" : "?"}target=${target}`
        : "";
    navigate(`${path}${targetSuffix}`);
  };

  return {
    current: t("nav.marketplace", "Extension"),
    center: (
      <div className={styles.navigation}>
        <Tabs
          className={styles.sectionSwitch}
          activeKey={activeSection}
          items={items}
          onChange={handleChange}
          type="segmented"
        />
      </div>
    ),
    extra,
  };
}
