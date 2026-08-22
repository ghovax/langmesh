"use client";

import { Button, Flex, Spinner, Text } from "@chakra-ui/react";
import { LuRotateCw, LuSquare } from "react-icons/lu";

/** One long-running harness activity — a goal review or a compaction summary being asked again
 * until it submits — said out loud with a spinner, and with the two things a person can do about it. */
export function ActivityStatus({
  label,
  cancelLabel,
  retryLabel,
  onCancel,
  onRetry,
}: {
  /** What is running, in one sentence the reader can act on. */
  label: string;
  cancelLabel?: string;
  retryLabel?: string;
  onCancel?: () => void;
  onRetry?: () => void;
}) {
  return (
    <Flex
      align="center"
      gap={2}
      flexShrink={0}
      minW={0}
      px={2}
      py={1}
      borderRadius="md"
      bg="bg.subtle"
      color="blue.fg"
    >
      <Spinner boxSize="0.9em" borderWidth="1.5px" color="blue.solid" flexShrink={0} />
      <Text fontSize="xs" whiteSpace="nowrap" overflow="hidden" textOverflow="ellipsis" minW={0}>
        {label}
      </Text>
      {onCancel && cancelLabel ? (
        <Button
          size="2xs"
          variant="ghost"
          flexShrink={0}
          onClick={onCancel}
          aria-label={cancelLabel}
        >
          <LuSquare size={10} />
          {cancelLabel}
        </Button>
      ) : null}
      {onRetry && retryLabel ? (
        <Button size="2xs" variant="ghost" flexShrink={0} onClick={onRetry} aria-label={retryLabel}>
          <LuRotateCw size={11} />
          {retryLabel}
        </Button>
      ) : null}
    </Flex>
  );
}
