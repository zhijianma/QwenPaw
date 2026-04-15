/**
 * view-image-plugin – QwenPaw frontend plugin
 *
 * Registers a custom renderer for the "view_image" tool.
 * When the tool is called, the full IAgentScopeRuntimeMessage is displayed
 * as a formatted JSON block so every field is visible.
 *
 * Build:
 *   npm install && npm run build
 *
 * Install:
 *   cp -r . ~/.qwenpaw/plugins/view-image-plugin
 */

import React from "react";

// ── Type definitions (mirrors @agentscope-ai/chat internals) ──────────────

interface IContent {
    type: string;
    data?: any;
    [key: string]: any;
}

/** Shape passed by the host app as the `data` prop */
interface IAgentScopeRuntimeMessage {
    id: string;
    object?: string;
    role: string;
    type: string;
    content: IContent[];
    status: string;
    code?: string;
    message?: string;
    [key: string]: any;
}

// ── URL helper ───────────────────────────────────────────────────────────

/**
 * Build the backend preview URL for a local file path.
 * Mirrors host-app chatApi.filePreviewUrl + getApiUrl so this standalone
 * UMD bundle needs no imports from the main app.
 */
function toPreviewUrl(rawPath: string): string {
    if (!rawPath) return "";
    if (rawPath.startsWith("http://") || rawPath.startsWith("https://"))
        return rawPath;

    let filePath = rawPath.startsWith("file://")
        ? rawPath.slice(7)
        : rawPath;
    if (!filePath.startsWith("/")) filePath = "/" + filePath;

    // Honour VITE_API_BASE_URL if the host app exposes it on window
    const base: string =
        typeof (window as any).__VITE_API_BASE_URL === "string"
            ? (window as any).__VITE_API_BASE_URL
            : "";
    const url = `${base}/api/files/preview/${filePath.replace(/^\/+/, "")}`;

    const token = localStorage.getItem("qwenpaw_auth_token") ?? "";
    return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

// ── ViewImageRender ──────────────────────────────────────────────────────

/**
 * Custom renderer for the "view_image" tool.
 *
 * - Parses arguments.image_path (may be a JSON string) from content[0]
 * - Renders the image via the backend /api/files/preview endpoint
 * - Shows a collapsible raw-JSON inspector below for debugging
 */
function ViewImageRender({ data }: { data: IAgentScopeRuntimeMessage }) {
    const [open, setOpen] = React.useState(false);

    const json = React.useMemo(
        () => JSON.stringify(data, null, 2),
        [data],
    );

    const firstData = data?.content?.[0]?.data;
    const toolName: string = firstData?.name ?? "view_image";

    // arguments may arrive as a JSON string or as a plain object
    const rawArgs = firstData?.arguments;
    const args: Record<string, any> =
        typeof rawArgs === "string"
            ? (() => { try { return JSON.parse(rawArgs); } catch { return {}; } })()
            : (rawArgs ?? {});

    const imagePath: string = args?.image_path ?? "";
    const imgUrl = toPreviewUrl(imagePath);

    return (
        <div style={styles.wrapper}>
            {/* ── image preview ─────────────────────────────────────────── */}
            {imgUrl && (
                <img
                    src={imgUrl}
                    alt={imagePath}
                    style={styles.img}
                    onError={(e: React.SyntheticEvent<HTMLImageElement>) => {
                        (e.currentTarget as HTMLImageElement).style.display = "none";
                    }}
                />
            )}

            {/* ── collapsible header / JSON inspector ───────────────────── */}
            <div style={styles.header} onClick={() => setOpen((v: boolean) => !v)}>
                <span style={styles.badge}>🖼 {toolName}</span>
                {imagePath && (
                    <span style={styles.subtitle}>{imagePath}</span>
                )}
                <span style={styles.toggle}>{open ? "▲ collapse" : "▼ expand"}</span>
            </div>

            {open && (
                <pre style={styles.pre}>{json}</pre>
            )}
        </div>
    );
}

// ── Inline styles (no external CSS dependency) ────────────────────────────

const styles: Record<string, React.CSSProperties> = {
    wrapper: {
        border: "1px solid #e0e0e0",
        borderRadius: 6,
        overflow: "hidden",
        fontSize: 13,
        fontFamily: "inherit",
        margin: "4px 0",
        background: "#fafafa",
    },
    img: {
        display: "block",
        maxWidth: "100%",
        maxHeight: 480,
        objectFit: "contain",
        background: "#000",
    },
    header: {
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 10px",
        cursor: "pointer",
        userSelect: "none",
        background: "#f0f0f0",
    },
    badge: {
        fontWeight: 600,
        whiteSpace: "nowrap",
    },
    subtitle: {
        flex: 1,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        color: "#666",
        fontSize: 12,
    },
    toggle: {
        marginLeft: "auto",
        color: "#999",
        fontSize: 11,
        whiteSpace: "nowrap",
    },
    pre: {
        margin: 0,
        padding: "10px 12px",
        overflowX: "auto",
        background: "#1e1e1e",
        color: "#d4d4d4",
        fontSize: 12,
        lineHeight: 1.5,
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
    },
};

// ── Plugin registration ───────────────────────────────────────────────────

(window as any).__registerPlugin?.(
    {
        name: "view-image-plugin",
        version: "1.0.0",
        description: "Renders view_image tool calls with a JSON inspector",
        entry: { frontend: "dist/index.umd.js" },
    },
    {
        messageTypes: {
            // Key must match the tool name reported by the backend
            view_image: ViewImageRender,
        },
    },
);
