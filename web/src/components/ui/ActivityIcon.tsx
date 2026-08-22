import { Span, Spinner, type SpinnerProps } from "@chakra-ui/react";
import { useLayoutEffect, useRef, type ReactNode } from "react";

const ACTIVITY_ICON_BOX_SIZE = "3.5";
const ACTIVITY_SPINNER_SIZE = "3";

export function ActivityIcon({ children }: { children: ReactNode }) {
  return (
    <Span
      boxSize={ACTIVITY_ICON_BOX_SIZE}
      display="inline-flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
      css={{
        "& > *": { width: "100%", height: "100%" },
        "& svg": { width: "100%", height: "100%" },
      }}
    >
      {children}
    </Span>
  );
}

export function ActivitySpinner(properties: SpinnerProps) {
  const spinnerRef = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const animation = spinnerRef.current?.getAnimations()[0];
    const timelineTime = document.timeline.currentTime;
    if (animation && typeof timelineTime === "number") animation.currentTime = timelineTime;
  }, []);

  return (
    <Span
      boxSize={ACTIVITY_ICON_BOX_SIZE}
      display="inline-flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
    >
      <Spinner
        ref={spinnerRef}
        boxSize={ACTIVITY_SPINNER_SIZE}
        borderWidth="1.5px"
        {...properties}
      />
    </Span>
  );
}
