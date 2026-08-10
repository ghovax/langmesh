"use client";

import { Box, Flex, VStack } from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { LuClipboardCheck } from "react-icons/lu";
import { useTranslations } from "next-intl";
import { ChatMessageItem, ChatToolGroup } from "@/components/chat-message";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { timelineItems } from "@/lib/chat-timeline";
import {
  appendTranscriptPart,
  createTranscriptState,
  replayTurns,
  type ChatMessage,
  type TranscriptState,
} from "@/lib/use-chat";
import {
  attachGoalReview,
  fetchGoalReviews,
  subscribeEvents,
  type GoalReviewSession,
} from "@/lib/api";
import { swallowed } from "@/lib/swallowed";

function GoalReviewTranscript({ review }: { review: GoalReviewSession }) {
  const translation = useTranslations("GoalReviewPanel");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [running, setRunning] = useState(review.status === "working");
  const [observedStatus, setObservedStatus] = useState(review.status);
  const transcriptRef = useRef<TranscriptState>(createTranscriptState());

  useEffect(() => {
    const attachment = attachGoalReview(
      review.review_id,
      (frame) => {
        if (frame.kind === "snapshot") {
          transcriptRef.current = replayTurns(frame.turns);
          setMessages([...transcriptRef.current.messages]);
          const snapshotStatus = frame.turns.at(-1)?.status?.state;
          if (
            snapshotStatus === "working" ||
            snapshotStatus === "completed" ||
            snapshotStatus === "canceled" ||
            snapshotStatus === "failed"
          ) {
            setObservedStatus(snapshotStatus);
            setRunning(snapshotStatus === "working");
          }
        } else if (frame.kind === "live") {
          appendTranscriptPart(transcriptRef.current, frame.part);
          setMessages([...transcriptRef.current.messages]);
        } else if (frame.kind === "turn") {
          setRunning(frame.running);
        }
      },
      () => undefined,
    );
    return attachment.abort;
  }, [review.review_id]);

  const timeline = useMemo(
    () =>
      timelineItems(
        messages.filter(
          (message) =>
            message.role !== "user" && message.role !== "peer" && message.role !== "goal",
        ),
      ),
    [messages],
  );
  const status = review.status === "working" ? observedStatus : review.status;
  const isRunning = status === "working" && running;
  return (
    <VStack align="stretch" gap={2.5} px={4} py={3}>
      {timeline.map((item, index) =>
        item.kind === "tool_group" ? (
          <ChatToolGroup
            key={item.id}
            messages={item.messages}
            thinkingTurns={item.thinkingTurns}
            keepOpen={isRunning && index === timeline.length - 1}
            pendingLabel={translation("reviewing")}
          />
        ) : (
          <Box key={item.message.id} display="flex" flexDirection="column">
            <ChatMessageItem
              message={item.message}
              streaming={isRunning && index === timeline.length - 1}
            />
          </Box>
        ),
      )}
    </VStack>
  );
}

export function GoalReviewPanel({
  sessionId,
  selectedReviewId,
  onSelectedReviewChange,
  onClose,
}: {
  sessionId: string | null;
  selectedReviewId: string | null;
  onSelectedReviewChange: (reviewId: string | null) => void;
  onClose?: () => void;
}) {
  const translation = useTranslations("GoalReviewPanel");
  const [reviews, setReviews] = useState<GoalReviewSession[]>([]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!sessionId) {
        setReviews([]);
        onSelectedReviewChange(null);
        return;
      }
      try {
        const found = await fetchGoalReviews(sessionId);
        if (cancelled) return;
        setReviews(found);
        const requested = found.some((review) => review.review_id === selectedReviewId)
          ? selectedReviewId
          : (found[0]?.review_id ?? null);
        if (requested !== selectedReviewId) onSelectedReviewChange(requested);
      } catch (caught) {
        swallowed({ component: "goal-review-panel", operation: "read goal reviews" }, caught);
      }
    };
    void load();
    const unsubscribe = subscribeEvents((event) => {
      const changed = event as { type: string; session?: string };
      if (changed.type === "goal_reviews_changed" && changed.session === sessionId) void load();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [onSelectedReviewChange, selectedReviewId, sessionId]);

  const selectedReview = reviews.find((review) => review.review_id === selectedReviewId) ?? null;

  return (
    <PanelCard>
      <PanelHeader
        icon={<LuClipboardCheck size={14} />}
        title={translation("title")}
        closeLabel={translation("collapse")}
        onClose={onClose}
      />
      <PanelBody px={0}>
        {reviews.length === 0 ? (
          <PanelEmptyState
            icon={<LuClipboardCheck />}
            title={translation("emptyTitle")}
            description={translation("emptyDescription")}
          />
        ) : (
          <Flex direction="column" minH="100%">
            {selectedReview ? (
              <GoalReviewTranscript key={selectedReview.review_id} review={selectedReview} />
            ) : null}
          </Flex>
        )}
      </PanelBody>
    </PanelCard>
  );
}
