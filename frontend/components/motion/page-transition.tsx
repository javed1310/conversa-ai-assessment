"use client";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
export function PageTransition({ identity }: { identity: string }) {
  const reduced = useReducedMotion();
  return (
    <AnimatePresence>
      <motion.div
        key={identity}
        className="page-curtain"
        aria-hidden="true"
        initial={{ scaleY: 1 }}
        animate={{ scaleY: 0 }}
        exit={{ opacity: 0 }}
        transition={{ duration: reduced ? 0 : 0.65, ease: [0.76, 0, 0.24, 1] }}
      />
    </AnimatePresence>
  );
}
