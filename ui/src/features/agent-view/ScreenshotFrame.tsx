import { artifactUrl } from "../../lib/constants";
import { truncate } from "../../lib/format";
import type { GraphState } from "../../types/graph";
import type { ExpandedScreenshot } from "./ScreenshotOverlay";

interface ScreenshotFrameProps {
  state: GraphState | null;
  onExpand: (screenshot: ExpandedScreenshot) => void;
}

export function ScreenshotFrame({ state, onExpand }: ScreenshotFrameProps) {
  const screenshot = artifactUrl(state?.screenshot);
  const url = state?.url ?? "about:blank";

  return (
    <div className="browser-frame">
      <div className="browser-frame__chrome">
        <span className="browser-frame__dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
        <span className="browser-frame__url" title={url}>
          {truncate(url, 70)}
        </span>
        {screenshot && (
          <button
            className="icon-button browser-frame__expand"
            onClick={() => onExpand({
              imageUrl: screenshot,
              pageUrl: url,
              title: state?.title || "Captured state",
            })}
            title="Expand screenshot"
            aria-label="Expand screenshot"
          >
            {"\u2922"}
          </button>
        )}
      </div>
      <div className="browser-frame__viewport" key={state?.id ?? "empty"}>
        {screenshot ? (
          <img src={screenshot} alt={state ? `Screenshot of ${state.title}` : "Agent viewport"} />
        ) : (
          <div className="browser-frame__placeholder">No screenshot captured yet</div>
        )}
      </div>
    </div>
  );
}
