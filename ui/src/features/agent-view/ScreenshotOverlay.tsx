import { useEffect, useState } from "react";

export interface ExpandedScreenshot {
  imageUrl: string;
  pageUrl: string;
  title: string;
}

interface ScreenshotOverlayProps {
  screenshot: ExpandedScreenshot | null;
  onClose: () => void;
}

export function ScreenshotOverlay({ screenshot, onClose }: ScreenshotOverlayProps) {
  const [actualSize, setActualSize] = useState(false);

  useEffect(() => {
    if (!screenshot) return;
    setActualSize(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [screenshot, onClose]);

  if (!screenshot) return null;

  return (
    <div className="overlay" onClick={onClose} role="dialog" aria-modal>
      <div className="overlay__frame" onClick={(e) => e.stopPropagation()}>
        <div className="overlay__chrome">
          <span className="browser-frame__dots" aria-hidden><i /><i /><i /></span>
          <span className="overlay__meta">
            <strong>{screenshot.title}</strong>
            <span>{screenshot.pageUrl}</span>
          </span>
          <button
            className="button button--ghost"
            type="button"
            onClick={() => setActualSize((value) => !value)}
          >
            {actualSize ? "Fit image" : "Actual size"}
          </button>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            {"\u00d7"}
          </button>
        </div>
        <div className="overlay__body">
          <img
            className={actualSize ? "overlay__image--actual" : undefined}
            src={screenshot.imageUrl}
            alt={`Expanded screenshot of ${screenshot.title}`}
          />
        </div>
      </div>
    </div>
  );
}
