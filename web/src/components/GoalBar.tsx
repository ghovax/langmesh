"use client";

import { Box, Button, Flex, List, Span, Spinner, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  LuCircleCheck,
  LuCircleSlash,
  LuClipboardCheck,
  LuDot,
  LuRotateCw,
  LuSquare,
  LuFlag,
  LuX,
} from "react-icons/lu";
import { Tooltip } from "./ui/Tooltip";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { InlineMarkdown, MarkdownContent } from "./MarkdownContent";
import type { SessionGoal } from "@/lib/api";

// What the session is working toward, above the composer because it is a state rather than an event.
export function GoalBar({
  goal,
  onClear,
  onResume,
  onOpenReview,
}: {
  goal: SessionGoal;
  onClear: () => void;
  onResume?: () => void;
  onOpenReview: () => void;
}) {
  const translation = useTranslations("GoalBar");
  const [confirming, setConfirming] = useState(false);
  const text = (goal.text ?? "").trim();
  if (!text) return null;

  const status = goal.status || "active";
  const reviewPhase = status === "active" ? goal.review_phase : "";
  // A resolved goal stops driving the session but stays on the record, so the bar keeps showing it.
  const resolved = status === "satisfied" || status === "cleared";
  // Each phase and settled status has its own colour, while the goal text keeps the plain foreground.
  const tone =
    reviewPhase === "waiting_for_background"
      ? "cyan.fg"
      : reviewPhase === "checking"
        ? "purple.fg"
        : status === "blocked"
          ? "red.fg"
          : status === "parked"
            ? "orange.fg"
            : status === "satisfied"
              ? "green.fg"
              : status === "cleared"
                ? "fg.muted"
                : "blue.fg";
  const statusLabel =
    reviewPhase === "waiting_for_background"
      ? translation("waitingForBackground")
      : reviewPhase === "checking"
        ? translation("checking")
        : status === "blocked"
          ? translation("blocked")
          : status === "parked"
            ? translation("waiting")
            : status === "satisfied"
              ? translation("satisfied")
              : status === "cleared"
                ? translation("cleared")
                : translation("working");

  // The requirements are the goal's substance, but taller than the bar, so they live in the hover card.
  const detail = (
    <Box whiteSpace="normal" maxW="360px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuFlag size={12} />
        <Text fontWeight="semibold">{statusLabel}</Text>
      </Flex>
      <Box mb={goal.requirements?.length || goal.blocker || goal.evidence ? 2 : 0}>
        <MarkdownContent content={text} fontSize="xs" />
      </Box>
      {!!goal.requirements?.length && (
        <Box>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("requirements")}
          </Text>
          <List.Root pl={4} fontSize="xs" listStyleType="disc">
            {goal.requirements.map((requirement, requirementIndex) => (
              <List.Item key={requirementIndex} mb={0.5} _last={{ mb: 0 }}>
                <MarkdownContent content={requirement} fontSize="xs" />
              </List.Item>
            ))}
          </List.Root>
        </Box>
      )}
      {/* What the review settled belongs here; its continuation message already lives in the chat. */}
      {!!goal.blocker && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("blocker")}
          </Text>
          <MarkdownContent content={goal.blocker} fontSize="xs" />
        </Box>
      )}
      {!!goal.evidence && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("evidence")}
          </Text>
          <MarkdownContent content={goal.evidence} fontSize="xs" />
        </Box>
      )}
    </Box>
  );

  return (
    <>
      <Flex
        align="center"
        gap={2}
        mb={3}
        px={3}
        py={2}
        borderRadius="md"
        borderWidth="1px"
        borderColor="border.muted"
        boxShadow="Panel"
        bg="bg.panel"
      >
        <Tooltip
          content={detail}
          rich
          openDelay={200}
          closeDelay={60}
          positioning={{ placement: "top" }}
        >
          <Flex align="center" gap={2} flex={1} minW={0} color={tone}>
            {/* Work in progress is shown as motion; a stopped goal is shown as the thing that stopped it. */}
            <Box
              display="flex"
              alignItems="center"
              justifyContent="center"
              flexShrink={0}
              boxSize="13px"
            >
              {status === "active" ? (
                <Spinner boxSize="13px" borderWidth="1.5px" color={tone} />
              ) : status === "satisfied" ? (
                <LuCircleCheck size={13} />
              ) : (
                <LuCircleSlash size={13} />
              )}
            </Box>
            {/* Keep the status, separator, and goal readable as one line. */}
            <Text
              textStyle="xs"
              fontWeight="medium"
              flexShrink={0}
              display={{ base: "none", sm: "block" }}
            >
              {statusLabel}
            </Text>
            <Box
              display={{ base: "none", sm: "flex" }}
              alignItems="center"
              flexShrink={0}
              // The glyph's box is mostly empty around a small dot, so the row's gap is pulled back in.
              mx="-9px"
            >
              <LuDot size={20} style={{ opacity: 0.7 }} />
            </Box>
            <Box textStyle="xs" color="fg" truncate minW={0}>
              <InlineMarkdown content={text} />
            </Box>
          </Flex>
        </Tooltip>
        {/* A live review is worth opening: the transcript panel shows the reviewer's
            reasoning and verdict as they happen. Parked goals keep the same control when
            a reviewer settled (or failed to), and hide it when the working agent is itself
            the settlement. */}
        {(reviewPhase || (status === "parked" && goal.settlement !== "agent")) && (
          <Button
            title={translation("viewReview")}
            size="2xs"
            variant="plain"
            px={1}
            flexShrink={0}
            color="blue.fg"
            onClick={onOpenReview}
          >
            <LuClipboardCheck size={13} />
            <Span textStyle="xs" display={{ base: "none", sm: "inline" }}>
              {translation("viewReview")}
            </Span>
          </Button>
        )}
        {status === "parked" && onResume ? (
          <Button
            title={translation("resume")}
            size="2xs"
            variant="plain"
            px={1}
            gap={1}
            flexShrink={0}
            color="blue.fg"
            onClick={onResume}
          >
            <LuRotateCw size={13} />
            <Span textStyle="xs" display={{ base: "none", sm: "inline" }}>
              {translation("resume")}
            </Span>
          </Button>
        ) : null}
        {/* Named as well as drawn: the one control here ends the thing the bar is about, so it says which. */}
        <Button
          title={translation(resolved ? "dismiss" : "stop")}
          size="2xs"
          variant="plain"
          px={1}
          gap={1}
          colorPalette={resolved ? "gray" : "red"}
          color={resolved ? "fg.subtle" : "red.fg"}
          flexShrink={0}
          onClick={() => (resolved ? onClear() : setConfirming(true))}
        >
          {resolved ? <LuX size={13} /> : <LuSquare size={13} />}
          {/* The goal itself yields the width first; on the narrowest bars the icon stands on its own. */}
          <Span textStyle="xs" display={{ base: "none", sm: "inline" }}>
            {translation(resolved ? "dismiss" : "stop")}
          </Span>
        </Button>
      </Flex>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={translation("stopTitle")}
        confirmLabel={translation("stopConfirm")}
        danger
        onConfirm={onClear}
      >
        {translation("stopBody")}
      </ConfirmDialog>
    </>
  );
}
