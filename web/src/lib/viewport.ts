"use client";

import { useSyncExternalStore } from "react";

export const COMPACT_VIEWPORT_QUERY = "(max-width: 767px)";

function subscribeCompactViewport(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const query = window.matchMedia(COMPACT_VIEWPORT_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function isCompactViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.matchMedia &&
    window.matchMedia(COMPACT_VIEWPORT_QUERY).matches
  );
}

export function useCompactViewport(): boolean {
  return useSyncExternalStore(subscribeCompactViewport, isCompactViewport, () => true);
}
