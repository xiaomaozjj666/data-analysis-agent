import { useEffect, useState } from "react";
import { API_URL } from "../constants";
import { requestHeaders } from "../utils/api";

// 受保护资源的图片加载组件：<img src="/api/..."> 无法携带自定义请求头，
// 启用 APP_ACCESS_TOKEN 时直接引用会 401 导致缩略图静默加载失败（实测复现）。
// 本组件用 fetch（带 X-App-Token 头）取回 blob 转 objectURL 渲染，
// 组件卸载时释放 objectURL，避免内存泄漏。
interface AuthImageProps {
  /** 以 /api/ 开头的受保护资源相对路径 */
  src: string;
  alt?: string;
  className?: string;
  loading?: "lazy" | "eager";
}

function AuthImage({ src, alt = "", className, loading = "lazy" }: AuthImageProps) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let createdUrl: string | null = null;
    setObjectUrl(null);
    setFailed(false);
    const controller = new AbortController();
    fetch(`${API_URL}${src}`, { headers: requestHeaders(), signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.blob();
      })
      .then((blob) => {
        if (disposed) return;
        createdUrl = URL.createObjectURL(blob);
        setObjectUrl(createdUrl);
      })
      .catch(() => {
        if (!disposed) setFailed(true);
      });
    return () => {
      disposed = true;
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [src]);

  if (failed || !objectUrl) return null;
  return <img src={objectUrl} alt={alt} className={className} loading={loading} />;
}

export default AuthImage;
