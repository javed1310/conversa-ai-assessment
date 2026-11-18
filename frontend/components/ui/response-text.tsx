"use client";
import { useEffect, useState } from "react";
export function ResponseText({ text }: { text: string }) {
  const [length, setLength] = useState(0);
  useEffect(() => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      requestAnimationFrame(() => setLength(text.length));
      return;
    }
    let frame = 0;
    let start = 0;
    const tick = (time: number) => {
      start ||= time;
      setLength(
        Math.min(text.length, Math.ceil(((time - start) / 650) * text.length)),
      );
      if (time - start < 650) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [text]);
  return (
    <p>
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">{text.slice(0, length)}</span>
    </p>
  );
}
