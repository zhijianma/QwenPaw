/**
 * AppMarket.tsx — "应用市场" tab for the App Center.
 *
 * Reuses the existing plugin-market proxy (`/plugins/market/search`) and the
 * `installPlugin` flow, filtered to UI extensions (category "frontend") so the
 * market surfaces installable PawApps. Mirrors the Plugin Market UX.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Button, Card, Empty, Input, Spin, Typography } from "antd";
import { AppWindow, Download, ExternalLink, Search } from "lucide-react";
import { useAppMessage } from "@/hooks/useAppMessage";
import {
  buildMarketDownloadUrl,
  fetchMarketPlugins,
  type MarketPluginEntry,
} from "@/api/modules/pluginMarket";
import { installPlugin } from "@/api/modules/plugin";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;

const APP_CATEGORY = "app";

function pickDescription(entry: MarketPluginEntry, language: string): string {
  const locales = entry.locales;
  if (!locales || Object.keys(locales).length === 0) return "";
  if (locales[language]) return locales[language].description;
  const prefix = language.split("-")[0].toLowerCase();
  for (const key of Object.keys(locales)) {
    if (key.toLowerCase().startsWith(prefix)) return locales[key].description;
  }
  if (locales.en) return locales.en.description;
  return Object.values(locales)[0]?.description ?? "";
}

interface AppMarketProps {
  onInstalled: () => void;
}

export function AppMarket({ onInstalled }: AppMarketProps) {
  const { t, i18n } = useTranslation();
  const { message } = useAppMessage();
  const tRef = useRef(t);
  tRef.current = t;

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [plugins, setPlugins] = useState<MarketPluginEntry[]>([]);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [installingId, setInstallingId] = useState<string | null>(null);

  const load = useCallback(async (keyword: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMarketPlugins({
        page_number: 1,
        page_size: 30,
        search: keyword || undefined,
        category: keyword ? undefined : APP_CATEGORY,
      });
      setPlugins(data.plugins ?? []);
    } catch {
      setError(
        tRef.current(
          "pluginManager.marketUnavailable",
          "App market is currently unavailable.",
        ),
      );
      setPlugins([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(search);
  }, [search, load]);

  const handleInstall = useCallback(
    async (entry: MarketPluginEntry) => {
      setInstallingId(entry.id);
      try {
        const result = await installPlugin(buildMarketDownloadUrl(entry), {
          force: true,
        });
        message.success(
          `${tRef.current("appCenter.installSuccess", "Installed")}: ${
            result.name
          }`,
        );
        onInstalled();
        setTimeout(() => window.location.reload(), 800);
      } catch (err) {
        message.error(
          err instanceof Error
            ? err.message
            : tRef.current("appCenter.installFailed", "Install failed"),
        );
      } finally {
        setInstallingId(null);
      }
    },
    [message, onInstalled],
  );

  const lang = i18n.language;

  return (
    <div>
      <div className={styles.marketToolbar}>
        <Input
          prefix={<Search size={14} />}
          placeholder={t("appCenter.searchMarket", "Search app market...")}
          value={searchInput}
          onChange={(e) => {
            setSearchInput(e.target.value);
            if (!e.target.value) setSearch("");
          }}
          onPressEnter={() => setSearch(searchInput)}
          className={styles.searchInput}
          allowClear
        />
      </div>

      {error && (
        <Alert
          type="warning"
          showIcon
          message={error}
          style={{ marginBottom: 16 }}
        />
      )}

      <Spin spinning={loading}>
        {!loading && plugins.length === 0 && !error ? (
          <Empty
            image={<AppWindow size={48} strokeWidth={1} />}
            description={t("appCenter.marketEmpty", "No apps found")}
            style={{ marginTop: 48 }}
          />
        ) : (
          <div className={styles.grid}>
            {plugins.map((entry) => (
              <Card key={entry.id} className={styles.appCard} hoverable>
                <div className={styles.appCardIcon}>
                  {entry.logo_url ? (
                    <img
                      src={entry.logo_url}
                      alt=""
                      className={styles.marketLogo}
                    />
                  ) : (
                    <AppWindow size={32} strokeWidth={1.5} />
                  )}
                </div>
                <div className={styles.marketCardBody}>
                  <Text strong className={styles.appCardTitle}>
                    {entry.display_name}
                  </Text>
                  <Paragraph
                    type="secondary"
                    className={styles.appCardDesc}
                    ellipsis={{ rows: 2 }}
                  >
                    {pickDescription(entry, lang) || "No description"}
                  </Paragraph>
                  <span className={styles.marketMeta}>
                    v{entry.version}
                    {entry.developer ? ` · ${entry.developer}` : ""}
                    {entry.downloads != null ? ` · ⬇ ${entry.downloads}` : ""}
                  </span>
                  <div className={styles.marketActions}>
                    <Button
                      type="primary"
                      size="small"
                      icon={<Download size={14} />}
                      loading={installingId === entry.id}
                      disabled={
                        installingId !== null && installingId !== entry.id
                      }
                      onClick={() => void handleInstall(entry)}
                    >
                      {t("appCenter.install", "Install")}
                    </Button>
                    {entry.details_url && (
                      <Button
                        size="small"
                        icon={<ExternalLink size={14} />}
                        onClick={() =>
                          window.open(entry.details_url!, "_blank")
                        }
                      >
                        {t("appCenter.details", "Details")}
                      </Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Spin>
    </div>
  );
}
