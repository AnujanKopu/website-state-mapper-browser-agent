import { useEffect } from "react";
import type { RefObject } from "react";

const POINTER_QUERY = "(hover: hover) and (pointer: fine)";
const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function motionAllowed(): boolean {
  return typeof window.matchMedia === "function"
    && window.matchMedia(POINTER_QUERY).matches
    && !window.matchMedia(MOTION_QUERY).matches;
}

/** Writes pointer coordinates to CSS custom properties without triggering React renders. */
export function usePointerSpotlight<T extends HTMLElement>(ref: RefObject<T | null>) {
  useEffect(() => {
    const element = ref.current;
    if (!element || !motionAllowed()) return;

    let frame = 0;
    const move = (event: PointerEvent) => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const bounds = element.getBoundingClientRect();
        element.style.setProperty("--pointer-x", `${event.clientX - bounds.left}px`);
        element.style.setProperty("--pointer-y", `${event.clientY - bounds.top}px`);
        element.style.setProperty("--pointer-opacity", "1");
      });
    };
    const leave = () => element.style.setProperty("--pointer-opacity", "0");

    element.addEventListener("pointermove", move, { passive: true });
    element.addEventListener("pointerleave", leave);
    return () => {
      window.cancelAnimationFrame(frame);
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerleave", leave);
    };
  }, [ref]);
}

/** Adds a restrained magnetic offset to one high-value control. */
export function useMagneticHover<T extends HTMLElement>(ref: RefObject<T | null>) {
  useEffect(() => {
    const element = ref.current;
    if (!element || !motionAllowed()) return;

    let frame = 0;
    const move = (event: PointerEvent) => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const bounds = element.getBoundingClientRect();
        const x = ((event.clientX - bounds.left) / bounds.width - 0.5) * 5;
        const y = ((event.clientY - bounds.top) / bounds.height - 0.5) * 4;
        element.style.setProperty("--magnetic-x", `${x.toFixed(2)}px`);
        element.style.setProperty("--magnetic-y", `${y.toFixed(2)}px`);
      });
    };
    const reset = () => {
      element.style.setProperty("--magnetic-x", "0px");
      element.style.setProperty("--magnetic-y", "0px");
    };

    element.addEventListener("pointermove", move, { passive: true });
    element.addEventListener("pointerleave", reset);
    return () => {
      window.cancelAnimationFrame(frame);
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerleave", reset);
    };
  }, [ref]);
}

/** Reveals annotated sections as they enter the landing-page scroll container. */
export function useScrollReveals<T extends HTMLElement>(ref: RefObject<T | null>) {
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const targets = Array.from(root.querySelectorAll<HTMLElement>("[data-reveal]"));
    if (typeof IntersectionObserver === "undefined" || !motionAllowed()) {
      targets.forEach((target) => target.classList.add("is-visible"));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            (entry.target as HTMLElement).classList.add("is-visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { root, rootMargin: "0px 0px -8%", threshold: 0.12 },
    );
    targets.forEach((target) => observer.observe(target));
    return () => observer.disconnect();
  }, [ref]);
}
