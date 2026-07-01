import { useEffect, useRef, useState } from "react";

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
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!screenshot) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    setActualSize(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab") return;
      const focusable = frameRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex='-1'])",
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      previouslyFocused?.focus();
    };
  }, [screenshot, onClose]);

  if (!screenshot) return null;

  return (
    <div className="overlay" onClick={onClose} role="dialog" aria-modal="true" aria-labelledby="overlay-title">
      <div ref={frameRef} className="overlay__frame" onClick={(e) => e.stopPropagation()}>
        <div className="overlay__chrome">
          <span className="overlay__meta">
            <strong id="overlay-title">{screenshot.title}</strong>
            <span>{screenshot.pageUrl}</span>
          </span>
          <button
            className="button button--ghost"
            type="button"
            onClick={() => setActualSize((value) => !value)}
          >
            {actualSize ? "Fit image" : "Actual size"}
          </button>
          <button ref={closeRef} className="icon-button" onClick={onClose} aria-label="Close">
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
