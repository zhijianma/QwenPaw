import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import InlineHelp from "@/components/InlineHelp";
import { useAutoSave } from "@/hooks/useAutoSave";
import { Button } from "@agentscope-ai/design";
import { Alert, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { SettingsPage } from "@/components/settings";
import { useVoiceTranscription } from "./useVoiceTranscription";
import {
  AudioModeCard,
  ProviderTypeCard,
  ProviderSelectCard,
  TranscriptionModelCard,
} from "./components";
import styles from "./index.module.less";

function VoiceTranscriptionPage() {
  const { t } = useTranslation();
  const reduced = useReducedMotion();
  const {
    loading,
    ready,
    error,
    audioMode,
    setAudioMode,
    providerType,
    setProviderType,
    selectedProviderId,
    setSelectedProviderId,
    transcriptionModel,
    setTranscriptionModel,
    localWhisperStatus,
    installLocalWhisper,
    enableLocalWhisper,
    availableProviders,
    showProviderSection,
    isLocalWhisper,
    isWhisperApi,
    fetchSettings,
    handleSave,
  } = useVoiceTranscription();

  const { schedule } = useAutoSave(() => handleSave(true));

  if (loading) {
    return (
      <SettingsPage header={{ current: t("voiceTranscription.title") }}>
        <div className={styles.page}>
          <div className={styles.centerState}>
            <Spin />
          </div>
        </div>
      </SettingsPage>
    );
  }

  if (error) {
    return (
      <SettingsPage header={{ current: t("voiceTranscription.title") }}>
        <div className={styles.voiceTranscriptionPage}>
          <div className={styles.loadError}>
            <Alert
              type="error"
              showIcon
              message={t("voiceTranscription.loadFailed")}
              action={
                <Button type="primary" onClick={() => void fetchSettings()}>
                  {t("common.retry")}
                </Button>
              }
            />
          </div>
        </div>
      </SettingsPage>
    );
  }

  if (!ready) {
    return (
      <SettingsPage header={{ current: t("voiceTranscription.title") }}>
        <div className={styles.page}>
          <div className={styles.centerState}>
            <Spin />
          </div>
        </div>
      </SettingsPage>
    );
  }

  return (
    <SettingsPage
      header={{
        extra: (
          <InlineHelp>
            {t(
              isLocalWhisper
                ? "voiceTranscription.transcriptionInfoDescLocal"
                : "voiceTranscription.transcriptionInfoDesc",
            )}
          </InlineHelp>
        ),
        current: t("voiceTranscription.title"),
      }}
    >
      <div className={styles.voiceTranscriptionPage}>
        <div className={styles.content}>
          <AudioModeCard
            audioMode={audioMode}
            onAudioModeChange={(value) => {
              setAudioMode(value);
              schedule();
            }}
            localWhisperStatus={localWhisperStatus}
          />

          <AnimatePresence initial={false}>
            {showProviderSection && (
              <motion.div
                key="provider"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={
                  reduced
                    ? { duration: 0 }
                    : { type: "spring", stiffness: 360, damping: 38 }
                }
                style={{ overflow: "hidden" }}
              >
                <ProviderTypeCard
                  providerType={providerType}
                  onProviderTypeChange={(value) => {
                    setProviderType(value);
                    schedule();
                  }}
                  isLocalWhisper={isLocalWhisper}
                  onInstall={() => void installLocalWhisper()}
                  onEnable={() => void enableLocalWhisper()}
                  localWhisperStatus={localWhisperStatus}
                />

                {isWhisperApi && (
                  <ProviderSelectCard
                    availableProviders={availableProviders}
                    selectedProviderId={selectedProviderId}
                    onProviderChange={(value) => {
                      setSelectedProviderId(value);
                      schedule();
                    }}
                  />
                )}

                {isWhisperApi && (
                  <TranscriptionModelCard
                    transcriptionModel={transcriptionModel}
                    onTranscriptionModelChange={(value) => {
                      setTranscriptionModel(value);
                      schedule();
                    }}
                  />
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </SettingsPage>
  );
}

export default VoiceTranscriptionPage;
