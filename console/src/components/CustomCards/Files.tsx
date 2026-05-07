import { Space } from "antd";
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
  console.log("[Files] Custom Files component rendered", props);
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls("bubble-files");

  const handleDownload = async (fileInfo: { name?: string; url?: string }) => {
    console.log("[Files] handleDownload called", fileInfo);

    if (!fileInfo.url) {
      console.warn("[Files] No URL provided");
      return;
    }

    // In pywebview desktop, use native APIs
    const pywebview = (window as any).pywebview;
    console.log(
      "[Files] pywebview detected:",
      !!pywebview,
      "has api:",
      !!pywebview?.api,
    );

    if (pywebview?.api) {
      // Construct full URL for both APIs
      const fullUrl = fileInfo.url.startsWith("http")
        ? fileInfo.url
        : `${window.location.origin}${fileInfo.url}`;

      console.log("[Files] Full URL:", fullUrl);

      // Option 1: Use native save dialog (recommended)
      if (pywebview.api.save_file) {
        console.log("[Files] Using pywebview.api.save_file");
        try {
          const saved = await pywebview.api.save_file(
            fullUrl,
            fileInfo.name || "download",
          );
          // False means the user cancelled the OS save dialog — not an error.
          if (!saved) {
            console.log("[Files] User cancelled file download");
          } else {
            console.log("[Files] File saved successfully");
          }
        } catch (error) {
          console.error("[Files] Failed to save file:", error);
        }
        return;
      }

      // Option 2: Open in external browser (fallback)
      if (pywebview.api.open_external_link) {
        console.log("[Files] Using pywebview.api.open_external_link");
        try {
          pywebview.api.open_external_link(fullUrl);
          console.log("[Files] Opened in external browser");
        } catch (error) {
          console.error("[Files] Failed to open external link:", error);
        }
        return;
      }
    }

    // Fallback for regular browsers: use window.open
    console.log("[Files] Using window.open (browser fallback)");
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
              />

              <>HHH</>
              {fileInfo.url && (
                <div
                  className={`${prefixCls}-download`}
                  onClick={() => handleDownload(fileInfo)}
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
