"use client";

import { AnimatePresence, motion } from "motion/react";
import type { CSSProperties, ReactNode } from "react";

// The only entrance the transcript may use: a row may fade in, and may never fade out.

export interface FadeInProps {
  children: ReactNode;
  // Skips the animation for rows restored from history, which were never new.
  animate?: boolean;
  seconds?: number;
  style?: CSSProperties;
}

export function FadeIn({ children, animate = true, seconds = 0.18, style }: FadeInProps) {
  return (
    <motion.div
      initial={animate ? { opacity: 0 } : false}
      animate={{ opacity: 1 }}
      transition={{ duration: seconds, ease: "easeOut" }}
      style={style}
    >
      {children}
    </motion.div>
  );
}

export const fadeSurfaceTransition = { duration: 0.15, ease: "easeOut" as const };

// A keyed surface that cross-fades when its contents are replaced: loading to a list, one conversation to another.
export function FadeSwitch({
  childKey,
  children,
  seconds = 0.15,
  style,
}: {
  childKey: string;
  children: ReactNode;
  seconds?: number;
  style?: CSSProperties;
}) {
  return (
    <AnimatePresence mode="sync">
      <motion.div
        key={childKey}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: seconds, ease: "easeOut" }}
        style={{
          display: "flex",
          flexDirection: "column",
          flex: 1,
          minHeight: 0,
          width: "100%",
          ...style,
        }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}
