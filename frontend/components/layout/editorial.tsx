"use client";
import Image from "next/image";
import {
  SplitText,
  Marquee,
  MagneticButton,
  PinnedSection,
  HorizontalRail,
} from "@/components/motion/primitives";
export const imagery = (business: string) =>
  business === "fitness-studio"
    ? ["/images/studio.jpg", "/images/movement.jpg"]
    : ["/images/learning.jpg", "/images/focus.jpg"];
export function ChatEntry({
  business,
  businessName,
  prompts,
  onStart,
  busy,
}: {
  business: string;
  businessName: string;
  prompts: string[][];
  onStart: (prompt?: string) => void;
  busy: boolean;
}) {
  const images = imagery(business);
  return (
    <>
      <section className="editorial-hero">
        <div className="hero-meta">
          <span className="eyebrow">
            A LITTLE CONTEXT. A WORLD OF POSSIBILITY.
          </span>
          <span className="eyebrow">01 — MADE FOR PEOPLE</span>
        </div>
        <div className="hero-composition">
          <h1>
            <SplitText lines={["GOOD", "CONVERSATIONS", "START HERE."]} />
          </h1>
          <div className="hero-photo" data-parallax>
            <Image
              src={images[0]}
              alt={
                business === "fitness-studio"
                  ? "A sunlit movement studio"
                  : "People learning and creating together"
              }
              fill
              sizes="(max-width:768px) 66vw, 38vw"
              fetchPriority="high"
              priority
            />
            <span className="photo-label">A NEW PERSPECTIVE ↗</span>
          </div>
          <span className="hero-star" aria-hidden="true">
            ✳
          </span>
        </div>
        <div className="hero-bottom">
          <p>
            Big questions. Small details.
            <br />
            Let’s find <strong className="highlight">your next step.</strong>
          </p>
          <MagneticButton
            className="pill-button"
            onClick={() => onStart()}
            disabled={busy}
          >
            {busy ? "Opening…" : "Let’s talk"}{" "}
            <span className="button-arrow">↗</span>
          </MagneticButton>
          <span className="eyebrow scroll-cue">SCROLL TO EXPLORE ↓</span>
        </div>
      </section>
      <Marquee text={businessName} />
      <PinnedSection>
        <div className="section-kicker">
          <span className="eyebrow">THREE WAYS IN</span>
          <h2>Your next move.</h2>
          <p>
            No perfect question needed.
            <br />
            Pick a starting point. We’ll take it from there.
          </p>
        </div>
        <div className="journey-grid">
          {prompts.map(([label, prompt], i) => (
            <button
              key={label}
              className={`journey-card tint-${i}`}
              disabled={busy}
              onClick={() => onStart(prompt)}
            >
              <span className="eyebrow">0{i + 1} / START A CONVERSATION</span>
              <span className="journey-symbol" aria-hidden="true">
                {["↗", "✳", "↔"][i]}
              </span>
              <h3>{label}</h3>
              <span className="journey-bottom">
                {prompt}
                <b>↗</b>
              </span>
            </button>
          ))}
        </div>
      </PinnedSection>
      <section className="moments-section">
        <div className="section-kicker">
          <span className="eyebrow">CURIOUS LOOKS GOOD ON YOU</span>
          <h2>Find your moment.</h2>
          <span className="eyebrow">SCROLL / CHOOSE / ASK ↔</span>
        </div>
        <HorizontalRail>
          {prompts.map(([label, prompt], i) => (
            <button
              className="moment"
              key={label}
              onClick={() => onStart(prompt)}
              disabled={busy}
              data-cursor="View"
            >
              <div className="moment-photo">
                <Image
                  src={images[i % 2]}
                  alt={
                    i % 2
                      ? "Space to focus on your next step"
                      : "A fresh start, together"
                  }
                  fill
                  loading="lazy"
                  sizes="(max-width:768px) 78vw, 40vw"
                />
                <Image
                  className="moment-alt"
                  src={images[(i + 1) % 2]}
                  alt=""
                  fill
                  loading="lazy"
                  sizes="(max-width:768px) 78vw, 40vw"
                />
              </div>
              <span className="eyebrow">0{i + 1} / A FRESH START</span>
              <h3>
                {label}
                <span>↗</span>
              </h3>
            </button>
          ))}
        </HorizontalRail>
      </section>
    </>
  );
}
export function Footer({ onNavigate }: { onNavigate: (tab: string) => void }) {
  return (
    <footer className="editorial-footer">
      <div className="footer-note">
        <span className="eyebrow">LESS FRICTION. MORE CONNECTION.</span>
        <p>
          The right information.
          <br />A more human next step.
        </p>
        <span className="footer-flower" aria-hidden="true">
          ✳
        </span>
      </div>
      <h2>
        ALWAYS HELPFUL.
        <br />
        EVERY TIME.
      </h2>
      <div className="footer-links">
        <a href="#top" className="wordmark">
          conversa<span>✳</span>
        </a>
        <nav aria-label="Footer navigation">
          {["Chat", "Conversations", "Leads", "Knowledge base"].map((t) => (
            <button key={t} onClick={() => onNavigate(t)}>
              {t} ↗
            </button>
          ))}
        </nav>
      </div>
      <div className="legal">
        <span>LOCAL WORKSPACE · NOTIFICATIONS ARE SIMULATED</span>
        <span>BUILT FOR BETTER CONVERSATIONS © {new Date().getFullYear()}</span>
        <a href="#top">BACK TO TOP ↑</a>
      </div>
    </footer>
  );
}
