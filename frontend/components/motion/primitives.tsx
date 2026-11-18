"use client";
import type { ReactNode, ButtonHTMLAttributes } from "react";
import { useRef } from "react";
export function SplitText({ lines }: { lines: string[] }) {
  return (
    <>
      {lines.map((line, i) => (
        <span className="line-mask" key={i}>
          <span data-reveal>{line}</span>
        </span>
      ))}
    </>
  );
}
export function Reveal({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className} data-reveal>
      {children}
    </div>
  );
}
export function MagneticButton({
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <button
      {...props}
      ref={ref}
      onPointerMove={(e) => {
        if (
          e.pointerType !== "mouse" ||
          matchMedia("(prefers-reduced-motion: reduce)").matches
        )
          return;
        const r = e.currentTarget.getBoundingClientRect();
        e.currentTarget.style.translate = `${(e.clientX - r.x - r.width / 2) * 0.12}px ${(e.clientY - r.y - r.height / 2) * 0.12}px`;
      }}
      onPointerLeave={() => {
        if (ref.current) ref.current.style.translate = "0px 0px";
      }}
    >
      {children}
    </button>
  );
}
export function Marquee({ text }: { text: string }) {
  return (
    <div className="marquee" role="region" aria-label={text}>
      <div aria-hidden="true">
        {Array.from({ length: 4 }, (_, i) => (
          <span key={i}>
            {text} <b>✳</b> GOOD QUESTIONS. REAL POSSIBILITIES. <b>✳</b>{" "}
          </span>
        ))}
      </div>
    </div>
  );
}
export function PinnedSection({ children }: { children: ReactNode }) {
  return (
    <section className="journey-section" data-pin>
      {children}
    </section>
  );
}
export function HorizontalRail({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; scroll: number; moved: boolean } | null>(
    null,
  );
  return (
    <div
      ref={ref}
      className="horizontal-rail"
      role="region"
      data-cursor="Drag"
      tabIndex={0}
      aria-label="Suggested questions; scroll horizontally"
      onPointerDown={(e) => {
        if (e.pointerType !== "mouse") return;
        drag.current = {
          x: e.clientX,
          scroll: e.currentTarget.scrollLeft,
          moved: false,
        };
      }}
      onPointerMove={(e) => {
        if (!drag.current || !e.buttons) return;
        const distance = e.clientX - drag.current.x;
        if (Math.abs(distance) > 6) drag.current.moved = true;
        e.currentTarget.scrollLeft = drag.current.scroll - distance;
      }}
      onPointerLeave={() => {
        drag.current = null;
      }}
      onClickCapture={(e) => {
        if (drag.current?.moved) {
          e.preventDefault();
          e.stopPropagation();
        }
        drag.current = null;
      }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          ref.current?.scrollBy({
            left: (e.key === "ArrowRight" ? 1 : -1) * 300,
            behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
              ? "instant"
              : "smooth",
          });
        }
      }}
    >
      {children}
    </div>
  );
}
