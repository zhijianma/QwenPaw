import { Space, message } from "antd";
import { Attachments } from "@agentscope-ai/chat";
import { createGlobalStyle } from "antd-style";
import { useProviderContext } from "@agentscope-ai/chat";
import { SparkDownloadLine } from "@agentscope-ai/icons";

const Style = createGlobalStyle`
.${(p) => p.theme.prefixCls}-bubble-files-file {
  position: relative;
}

.${(p) => p.theme.prefixCls}-bubble-files-download {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0,0,0,0.5);
  z-index: 1;
  opacity: 0;
  font-size: 16px;
  border-radius: ${(p) => p.theme.borderRadius}px;
  cursor: pointer;
  color: ${(p) => p.theme.colorWhite};
  transition: opacity ${(p) => p.theme.motionDurationSlow}

}

.${(p) => p.theme.prefixCls}-bubble-files-file:hover .${(p) =>
  p.theme.prefixCls}-bubble-files-download {
  opacity: 1;
}
`;

interface FileInfo {
  name?: string;
  filename?: string;
  size?: number;
  bytes?: number;
  url?: string;
}

interface FilesProps {
  data: FileInfo[];
}

export default function Files(props: FilesProps) {
  // Helper to log and notify
  const debugLog = (
    msg: string,
    data?: any,
    level: "info" | "success" | "error" | "warning" = "info",
  ) => {
    console.log(msg, data || "");

    // Show message notification for important events
    if (
      msg.includes("handleDownload") ||
      msg.includes("clicked") ||
      msg.includes("saved") ||
      msg.includes("Failed")
    ) {
      if (level === "error") {
        message.error(msg);
      } else if (level === "success") {
        message.success(msg);
      } else if (level === "warning") {
        message.warning(msg);
      } else {
        message.info(msg);
      }
    }
  };

  console.log("[Files] Custom Files component rendered", props);
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls("bubble-files");

  const handleDownload = async (fileInfo: { name?: string; url?: string }) => {
    debugLog(
      `[Files] 点击下载: ${fileInfo.name || "unknown"}`,
      fileInfo,
      "info",
    );

    if (!fileInfo.url) {
      debugLog("[Files] 错误: 没有提供文件URL", null, "error");
      return;
    }

    // In pywebview desktop, use native APIs
    const pywebview = (window as any).pywebview;
    const isPywebview = !!pywebview?.api;
    console.log(`[Files] pywebview 检测: ${isPywebview}`);

    if (pywebview?.api) {
      // Construct full URL for both APIs
      const fullUrl = fileInfo.url.startsWith("http")
        ? fileInfo.url
        : `${window.location.origin}${fileInfo.url}`;

      console.log("[Files] Full URL:", fullUrl);

      // Option 1: Use native save dialog (recommended)
      if (pywebview.api.save_file) {
        console.log("[Files] 使用 pywebview.api.save_file");
        try {
          const saved = await pywebview.api.save_file(
            fullUrl,
            fileInfo.name || "download",
          );
          // False means the user cancelled the OS save dialog — not an error.
          if (!saved) {
            debugLog("用户取消了文件下载", null, "warning");
          } else {
            debugLog(`文件已保存: ${fileInfo.name}`, null, "success");
          }
        } catch (error) {
          debugLog(`保存文件失败: ${error}`, error, "error");
        }
        return;
      }

      // Option 2: Open in external browser (fallback)
      if (pywebview.api.open_external_link) {
        console.log("[Files] 使用 pywebview.api.open_external_link");
        try {
          pywebview.api.open_external_link(fullUrl);
          debugLog("已在外部浏览器打开", null, "info");
        } catch (error) {
          debugLog(`打开外部链接失败: ${error}`, error, "error");
        }
        return;
      }
    }

    // Fallback for regular browsers: use window.open
    console.log("[Files] 使用浏览器 window.open");
    window.open(fileInfo.url, "_blank");
  };

  return (
    <>
      <Style />
      <Space className={prefixCls}>
        {props.data.map((file, index) => {
          const fileInfo = {
            name: file.name || file.filename,
            size: file.size || file.bytes,
            url: file.url,
          };

          return (
            <div key={index} className={`${prefixCls}-file`}>
              <Attachments.FileCard
                // @ts-ignore
                item={fileInfo}
                onClick={() => {
                  debugLog(`文件卡片被点击: ${fileInfo.name}`, null, "info");
                  handleDownload(fileInfo);
                }}
              />

              {fileInfo.url && (
                <div
                  className={`${prefixCls}-download`}
                  onClick={(e) => {
                    e.stopPropagation();
                    debugLog(`下载图标被点击: ${fileInfo.name}`, null, "info");
                    handleDownload(fileInfo);
                  }}
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    background: "rgba(0,0,0,0.5)",
                    zIndex: 10,
                    opacity: 0,
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => {
                    (e.target as HTMLElement).style.opacity = "1";
                  }}
                  onMouseLeave={(e) => {
                    (e.target as HTMLElement).style.opacity = "0";
                  }}
                >
                  <SparkDownloadLine />
                </div>
              )}
            </div>
          );
        })}
      </Space>
    </>
  );
}
