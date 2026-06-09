/**
 * MinIO 预签名路径挂在站点根路径 /bisheng、/tmp-dir 下，
 * 不能拼 BASE_URL（如 /workspace），否则签名路径不匹配导致 400。
 */
export function buildFileAccessUrl(path: string): string {
    if (!path) return "";
    if (/^https?:\/\//i.test(path)) return path;

    const normalized = path.startsWith("/") ? path : `/${path}`;
    if (
        normalized.startsWith("/api/")
        || normalized.startsWith("/workspace/api/")
        || normalized.startsWith("/bisheng/")
        || normalized.startsWith("/tmp-dir/")
    ) {
        return `${window.location.origin}${normalized}`;
    }

    const base = (__APP_ENV__.BASE_URL || "").replace(/\/$/, "");
    return `${window.location.origin}${base}${normalized}`;
}

/** 判定预览组件类型：API /content 路径无扩展名，必须优先用 file_ext / 文件名 / type 参数 */
export function resolvePreviewFileType(
    fileName: string,
    options: { typeParam?: string; apiFileExt?: string; url?: string; fallback?: string } = {},
): string {
    const { typeParam = "", apiFileExt = "", url = "", fallback = "txt" } = options;

    if (apiFileExt) return apiFileExt.toLowerCase();
    if (typeParam) return typeParam.toLowerCase();

    const dotIndex = fileName.lastIndexOf(".");
    if (dotIndex > 0 && dotIndex < fileName.length - 1) {
        return fileName.substring(dotIndex + 1).toLowerCase();
    }

    // /content 等 API 路径不含扩展名，勿从 URL 猜测
    if (url && !url.includes("/content")) {
        try {
            const pathOnly = url.split("?")[0].split("#")[0];
            const lastSegment = pathOnly.split("/").pop() || "";
            const extIndex = lastSegment.lastIndexOf(".");
            if (extIndex >= 0 && extIndex < lastSegment.length - 1) {
                return lastSegment.substring(extIndex + 1).toLowerCase();
            }
        } catch {
            // ignore
        }
    }

    return fallback;
}
