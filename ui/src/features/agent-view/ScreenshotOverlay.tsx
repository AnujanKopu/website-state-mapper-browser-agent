import { useEffect } from "react";

interface ScreenshotOverlayProps {
  url: string | null;
  onClose: () => void;
}

export function ScreenshotOverlay({ url, onClose }: ScreenshotOverlayProps) {
  useEffect(() => {
    if (!url) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [url, onClose]);

  if (!url) return null;

  return (
    <div className="overlay" onClick={onClose} role="dialog" aria-modal>
      <div className="overlay__frame" onClick={(e) => e.stopPropagation()}>
        <div className="overlay__chrome">
          <span>Screenshot</span>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            {"\u00d7"}
          </button>
        </div>
        <div className="overlay__body">
          <img src={url} alt="Expanded screenshot" />
        </div>
      </div>
    </div>
  );
}
