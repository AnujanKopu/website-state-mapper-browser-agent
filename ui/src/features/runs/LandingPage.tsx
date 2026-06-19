import { useEffect, useRef, useState } from "react";

import flowstateIcon from "../../assets/flowstate-icon.png";
import { usePointerSpotlight, useScrollReveals } from "../../lib/pointerMotion";
import { LandingForm } from "./LandingForm";
import { CapabilityMosaic, MappingWalkthrough } from "./LandingShowcase";

interface LandingPageProps {
  onStarted: (runId: string) => void;
}

const HERO_HEADLINES = [
  { lead: "Map what lives", accent: "between the pages" },
  { lead: "See every", accent: "state" },
  { lead: "Trace every", accent: "user path" },
  { lead: "Surface every", accent: "product boundary" },
];

export function LandingPage({ onStarted }: LandingPageProps) {
  const pageRef = useRef<HTMLDivElement>(null);
  const [headlineIndex, setHeadlineIndex] = useState(0);

  usePointerSpotlight(pageRef);
  useScrollReveals(pageRef);

  useEffect(() => {
    const timer = window.setInterval(
      () => setHeadlineIndex((index) => (index + 1) % HERO_HEADLINES.length),
      4200,
    );
    return () => window.clearInterval(timer);
  }, []);

  const focusMapper = () => {
    const input = document.getElementById("target-url");
    input?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => input?.focus({ preventScroll: true }), 450);
  };

  return (
    <div className="landing" ref={pageRef}>
      <header className="landing__header">
        <a className="wordmark" href="#top" aria-label="FlowState home">
          <img className="wordmark__icon" src={flowstateIcon} alt="" aria-hidden />
          FlowState
        </a>
        <nav className="landing__nav" aria-label="Homepage sections">
          <a href="#capabilities">Capabilities</a>
          <a href="#how-it-works">How it works</a>
          <a href="#system">System</a>
        </nav>
        <button type="button" className="button button--ghost landing__header-cta" onClick={focusMapper}>
          Start a run <span aria-hidden>↗</span>
        </button>
      </header>

      <main>
        <section className="landing-hero" id="top">
          <div className="landing-hero__grid" aria-hidden />
          <div className="landing-hero__content">
            <h1 className="landing__title">
              <span className="hero-headlines" aria-hidden>
                {HERO_HEADLINES.map((headline, index) => (
                  <span
                    key={headline.accent}
                    className={`hero-headline${headlineIndex === index ? " is-active" : ""}`}
                  >
                    <span>{headline.lead}</span>
                    <span className="hero-headline__accent">
                      {headline.accent}<i aria-hidden />
                    </span>
                  </span>
                ))}
              </span>
              <span className="sr-only">See every state.</span>
            </h1>
            <p className="landing__subtitle">
              One URL becomes a live map of pages, modals, forms, auth walls, and the actions
              that connect them. Watch the agent find the product users actually experience.
            </p>
            <LandingForm onStarted={onStarted} />
          </div>
          <a className="landing-hero__scroll" href="#capabilities"><span>Scroll to explore</span><i aria-hidden /></a>
        </section>

        <section className="landing-section landing-section--showcase" id="capabilities" data-reveal>
          <div className="landing-section__heading">
            <p>Inside every run</p>
            <h2>Watch the map take shape.</h2>
            <span>Not a crawler report. A live, visual model of the reachable product.</span>
          </div>
          <CapabilityMosaic />
        </section>

        <section className="landing-section landing-section--process" id="how-it-works" data-reveal>
          <div className="landing-section__heading landing-section__heading--split">
            <div><p>How it works</p><h2>From URL to system map.</h2></div>
            <span>Four observable stages. Every decision stays legible.</span>
          </div>
          <MappingWalkthrough />
        </section>

        <section className="landing-metrics" id="system" data-reveal>
          <div className="landing-metric"><strong>12</strong><span>state types understood</span></div>
          <div className="landing-metric"><strong>13</strong><span>typed live event kinds</span></div>
          <div className="landing-metric"><strong>3</strong><span>export formats ready</span></div>
        </section>

        <section className="landing-final" data-reveal>
          <div className="landing-final__trace" aria-hidden />
          <p>Ready when you are</p>
          <h2>Give the agent a URL.<br />Get the whole product back.</h2>
          <button type="button" className="button button--primary" onClick={focusMapper}>
            Start mapping <span aria-hidden>↑</span>
          </button>
        </section>
      </main>

      <footer className="landing-footer">
        <span className="wordmark">
          <img className="wordmark__icon" src={flowstateIcon} alt="" aria-hidden />
          FlowState
        </span>
        <span>Screenshot-driven · SSE live · Safety-aware</span>
        <a href="#top">Back to top ↑</a>
      </footer>
    </div>
  );
}
