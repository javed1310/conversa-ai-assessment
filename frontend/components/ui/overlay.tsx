"use client";
import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

export function Overlay({
  open,
  onClose,
  title,
  children,
  className = "",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const old = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const node = ref.current;
    const background = Array.from(document.body.children).filter((element): element is HTMLElement => element instanceof HTMLElement && !element.contains(node));
    const previousInert = background.map(element => [element, element.inert] as const);
    background.forEach(element => { element.inert = true; });
    const focusable = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>(
          'button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]',
        ) ?? [],
      ).filter((e) => e.getClientRects().length);
    const timer = requestAnimationFrame(() => focusable()[0]?.focus());
    function key(e: KeyboardEvent) {
      if (
        document
          .querySelectorAll('[role="dialog"]')
          .item(document.querySelectorAll('[role="dialog"]').length - 1) !==
        node
      )
        return;
      if (e.key === "Escape") {
        e.preventDefault();
        close.current();
      }
      if (e.key === "Tab") {
        const items = focusable();
        const first = items[0],
          last = items.at(-1);
        if (!first) {
          e.preventDefault();
          node?.focus();
        } else if (
          e.shiftKey &&
          (document.activeElement === first ||
            !node?.contains(document.activeElement))
        ) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      cancelAnimationFrame(timer);
      document.body.style.overflow = old;
      document.removeEventListener("keydown", key);
      previousInert.forEach(([element, inert]) => { element.inert = inert; });
      previous?.focus();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div
      className={`overlay ${className}`}
      ref={ref}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-labelledby={id}
      data-lenis-prevent
    >
      <header className="overlay-heading">
        <span id={id} className="eyebrow">
          {title}
        </span>
        <button
          className="round-button"
          onClick={onClose}
          aria-label={`Close ${title}`}
        >
          ✕
        </button>
      </header>
      {children}
    </div>,
    document.body,
  );
}
