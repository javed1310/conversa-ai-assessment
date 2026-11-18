"use client";
import { useEffect, useRef } from "react";
export default function MotionRuntime() {
  const cursor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cleanup = () => {};
    let disposed = false;
    async function init() {
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const [{ gsap }, { ScrollTrigger }, { default: Lenis }] =
        await Promise.all([
          import("gsap"),
          import("gsap/ScrollTrigger"),
          import("lenis"),
        ]);
      if (disposed) return;
      gsap.registerPlugin(ScrollTrigger);
      const lenis = new Lenis({
        duration: 1.05,
        smoothWheel: true,
        anchors: true,
        prevent: (node) =>
          !!node.closest(
            "[data-lenis-prevent],.messages,.horizontal-rail,.record-list,.detail",
          ),
      });
      lenis.on("scroll", ScrollTrigger.update);
      const tick = (time: number) => lenis.raf(time * 1000);
      gsap.ticker.add(tick);
      const ctx = gsap.context(() => {
        ScrollTrigger.batch("[data-reveal]", {
          onEnter: (els) =>
            gsap.fromTo(
              els,
              { y: 48, opacity: 0 },
              {
                y: 0,
                opacity: 1,
                duration: 0.8,
                stagger: 0.07,
                ease: "power3.out",
                overwrite: true,
              },
            ),
          once: true,
        });
        gsap.utils
          .toArray<HTMLElement>("[data-parallax]")
          .forEach((el) =>
            gsap.to(el, {
              yPercent: -12,
              ease: "none",
              scrollTrigger: {
                trigger: el,
                start: "top bottom",
                end: "bottom top",
                scrub: 1,
              },
            }),
          );
        if (matchMedia("(min-width: 1024px)").matches)
          gsap.utils.toArray<HTMLElement>("[data-pin]").forEach((el) => {
            const cards = el.querySelectorAll(".journey-card");
            gsap.fromTo(
              cards,
              { scale: 0.94, y: 48 },
              {
                scale: 1,
                y: 0,
                stagger: 0.18,
                scrollTrigger: {
                  trigger: el,
                  start: "top 12%",
                  end: "+=360",
                  pin: true,
                  scrub: 1,
                },
              },
            );
          });
      });
      const visible = new IntersectionObserver((entries) =>
        entries.forEach((entry) =>
          (entry.target as HTMLElement).style.setProperty(
            "--animation-state",
            entry.isIntersecting ? "running" : "paused",
          ),
        ),
      );
      document
        .querySelectorAll(".marquee")
        .forEach((el) => visible.observe(el));
      const resize = () => ScrollTrigger.refresh();
      window.addEventListener("resize", resize);
      const move = (e: PointerEvent) => {
        if (!cursor.current) return;
        cursor.current.style.transform = `translate3d(${e.clientX}px,${e.clientY}px,0)`;
        const target = (e.target as HTMLElement).closest(
          "button,a,[data-cursor]",
        );
        cursor.current.dataset.hover = String(!!target);
        cursor.current.textContent = target?.getAttribute("data-cursor") ?? "";
      };
      window.addEventListener("pointermove", move, { passive: true });
      cleanup = () => {
        visible.disconnect();
        window.removeEventListener("resize", resize);
        window.removeEventListener("pointermove", move);
        ctx.revert();
        gsap.ticker.remove(tick);
        lenis.destroy();
      };
    }
    const begin = () => { void init(); };
    window.addEventListener("scroll", begin, { once: true, passive: true });
    return () => {
      window.removeEventListener("scroll", begin);
      disposed = true;
      cleanup();
    };
  }, []);
  return (
    <>
      <div className="scroll-progress" />
      <div ref={cursor} className="custom-cursor" aria-hidden="true" />
    </>
  );
}
