import { useState } from "react";

const CAPTURES = [
  { label: "Homepage", url: "acme.test", type: "page" },
  { label: "Pricing dialog", url: "acme.test/pricing", type: "modal" },
  { label: "Checkout boundary", url: "acme.test/checkout", type: "risky" },
];

const GRAPH_NODES = [
  { id: "s0", label: "Home", type: "PAGE", x: 7, y: 45 },
  { id: "s1", label: "Pricing", type: "PAGE", x: 38, y: 14 },
  { id: "s2", label: "Sign up", type: "MODAL", x: 38, y: 65 },
  { id: "s3", label: "Checkout", type: "RISKY", x: 72, y: 36 },
];

export function CapabilityMosaic() {
  const [captureIndex, setCaptureIndex] = useState(0);
  const [selectedNode, setSelectedNode] = useState("s2");
  const [exportType, setExportType] = useState<"MD" | "JSON" | "GRAPH">("MD");
  const capture = CAPTURES[captureIndex];

  return (
    <div className="capability-grid">
      <article className="capability-card capability-card--agent">
        <header><span>Agent viewport</span><i className="live-dot" aria-hidden /> Live capture</header>
        <div className="demo-browser">
          <div className="demo-browser__bar"><span className="browser-frame__dots" aria-hidden><i /><i /><i /></span><code>{capture.url}</code></div>
          <div className={`demo-browser__page demo-browser__page--${capture.type}`}>
            <div className="demo-browser__nav"><i /><i /><i /></div>
            <div className="demo-browser__hero"><span /><strong>{capture.label}</strong><i /></div>
            <div className="demo-browser__rows"><i /><i /><i /></div>
            {capture.type === "modal" && <div className="demo-browser__modal"><span>Start a trial</span><i /></div>}
            {capture.type === "risky" && <div className="demo-browser__guard"><span>Payment action denied</span><i>Safety rule</i></div>}
          </div>
        </div>
        <div className="card-switcher" aria-label="Preview captured states">
          {CAPTURES.map((item, index) => (
            <button key={item.label} type="button" className={index === captureIndex ? "is-active" : ""} onClick={() => setCaptureIndex(index)}>
              <span>0{index + 1}</span>{item.label}
            </button>
          ))}
        </div>
      </article>

      <article className="capability-card capability-card--graph">
        <header><span>Live topology</span><span>{selectedNode} selected</span></header>
        <div className="demo-graph">
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
            <path d="M18 52 C28 52 27 25 42 25" /><path d="M18 52 C27 52 28 75 42 75" />
            <path d="M56 25 C66 25 65 44 76 44" /><path d="M56 75 C66 75 65 48 76 48" />
          </svg>
          {GRAPH_NODES.map((node) => (
            <button
              key={node.id}
              type="button"
              className={`demo-node demo-node--${node.type.toLowerCase()}${selectedNode === node.id ? " is-active" : ""}`}
              style={{ left: `${node.x}%`, top: `${node.y}%` }}
              onClick={() => setSelectedNode(node.id)}
              aria-pressed={selectedNode === node.id}
            >
              <small>{node.type}</small><strong>{node.label}</strong><span>{node.id}</span>
            </button>
          ))}
        </div>
        <footer><span className="spectrum-key" aria-hidden /> Click a node to inspect its state</footer>
      </article>

      <article className="capability-card capability-card--safety">
        <header><span>Safety boundaries</span><span>Always on</span></header>
        <div className="safety-stack">
          <div><i className="safety-icon safety-icon--ok">✓</i><span><strong>Opened pricing</strong><small>safe navigation · explored</small></span></div>
          <div><i className="safety-icon safety-icon--warn">!</i><span><strong>Submit payment</strong><small>financial action · denied</small></span></div>
          <div><i className="safety-icon safety-icon--muted">↺</i><span><strong>Existing homepage</strong><small>duplicate state · merged</small></span></div>
        </div>
        <p>The agent maps risky boundaries without crossing them.</p>
      </article>

      <article className="capability-card capability-card--export">
        <header><span>Context-ready output</span><span>Portable</span></header>
        <div className="export-tabs" role="tablist" aria-label="Export preview">
          {(["MD", "JSON", "GRAPH"] as const).map((type) => (
            <button key={type} type="button" role="tab" aria-selected={exportType === type} className={exportType === type ? "is-active" : ""} onClick={() => setExportType(type)}>{type}</button>
          ))}
        </div>
        <pre aria-live="polite">{exportType === "MD"
          ? "# Product map\n\n5 states · 4 edges\n\n## Critical path\nHome → Pricing → Sign up"
          : exportType === "JSON"
            ? '{\n  "states": 5,\n  "edges": 4,\n  "status": "done"\n}'
            : "s0 ──pricing──▶ s2\n │               │\n └──docs──▶ s3   └──▶ s4"}</pre>
        <p>Hand the map to people, tools, or another agent.</p>
      </article>
    </div>
  );
}

type StageKey = "capture" | "classify" | "connect" | "export";

const STAGES: Array<{ key: StageKey; index: string; label: string; title: string; copy: string }> = [
  { key: "capture", index: "01", label: "Capture", title: "See the product as a user does.", copy: "The latest screenshot becomes the live viewport while the agent discovers actionable surfaces." },
  { key: "classify", index: "02", label: "Classify", title: "Name the state, not just the URL.", copy: "Pages, modals, tabs, forms, paywalls, auth walls, and risky terminals remain distinct." },
  { key: "connect", index: "03", label: "Connect", title: "Keep the path that made it reachable.", copy: "Every edge records the concrete action that moved the browser from one state to the next." },
  { key: "export", index: "04", label: "Export", title: "Turn exploration into usable context.", copy: "Download the graph or a compact context pack for engineering, QA, and product analysis." },
];

export function MappingWalkthrough() {
  const [active, setActive] = useState<StageKey>("capture");
  const stage = STAGES.find((item) => item.key === active) ?? STAGES[0];

  return (
    <div className="walkthrough">
      <div className="walkthrough__tabs" role="tablist" aria-label="Mapping stages">
        {STAGES.map((item) => (
          <button key={item.key} type="button" role="tab" aria-selected={active === item.key} className={active === item.key ? "is-active" : ""} onClick={() => setActive(item.key)}>
            <span>{item.index}</span><strong>{item.label}</strong><i aria-hidden>↗</i>
          </button>
        ))}
      </div>
      <div className={`walkthrough__stage walkthrough__stage--${stage.key}`} role="tabpanel">
        <div className="walkthrough__copy" key={stage.key}>
          <span>{stage.index} / 04</span><h3>{stage.title}</h3><p>{stage.copy}</p>
        </div>
        <StageVisual stage={stage.key} />
      </div>
    </div>
  );
}

function StageVisual({ stage }: { stage: StageKey }) {
  if (stage === "capture") return <div className="stage-visual stage-visual--capture"><div className="stage-cursor">↖</div><div className="stage-window"><i /><i /><i /><span /></div><div className="stage-scan" /></div>;
  if (stage === "classify") return <div className="stage-visual stage-visual--classify">{["PAGE", "MODAL", "FORM", "AUTH WALL", "PAYWALL", "DEAD END"].map((type) => <span key={type}>{type}</span>)}</div>;
  if (stage === "connect") return <div className="stage-visual stage-visual--connect"><i className="n1" /><i className="n2" /><i className="n3" /><span className="e1">Clicked Pricing</span><span className="e2">Opened signup</span></div>;
  return <div className="stage-visual stage-visual--export"><code>{'{ "run": "done",'}</code><code>{'  "states": 12,'}</code><code>{'  "edges": 18 }'}</code><span>JSON</span><span>MD</span></div>;
}
