"use client";

import { Box, Button, Flex, Separator, Span, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  LuCheck,
  LuClipboardCheck,
  LuClock,
  LuCopy,
  LuFlag,
  LuFoldVertical,
  LuMessagesSquare,
  LuRotateCw,
  LuTrash2,
  LuTriangleAlert,
} from "react-icons/lu";
import { reportError } from "@/lib/faults";
import { RelativeTime } from "@/components/ui/RelativeTime";
import type { ChatMessage, MessageAttachment } from "@/lib/use-chat";
import type { ToolEvent, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { toolCallReady, toolStatus } from "@/lib/tool-event";
import { AttachmentChips } from "./AttachmentChips";
import { MarkdownContent } from "./MarkdownContent";
import { ToolCall } from "./ToolCall";
import { ToolGroup } from "./ToolGroup";
import { ActivityIcon, ActivitySpinner } from "./ui/ActivityIcon";

interface ChatMessageProps {
  message: ChatMessage;
  // Re-run the turn that produced a server error, wired only for error rows.
  onRetry?: () => void;
  retrying?: boolean;
  // This is the last row of a live turn, which drives the newly-arrived-token animation.
  streaming?: boolean;
  // The review whose transcript this row opens, when it is not the row's own meta.
  reviewId?: string;
  onOpenReview?: (reviewId: string) => void;
}

// A server or turn error rendered as its own block rather than disguised as an assistant message.
function ErrorMessageCard({
  message,
  onRetry,
  retrying = false,
}: {
  message: ChatMessage;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const translation = useTranslations("ChatMessage");
  const error = message.meta?.error;
  if (!error) return null;
  const title = translation(`errors.${error.code}.title`);
  const body = translation(`errors.${error.code}.body`, error.parameters);
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="red.muted"
      bg="red.subtle"
      borderRadius="md"
      px={2.5}
      py={2.5}
    >
      <Flex align="center" gap={2} color="red.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text textStyle="panelTitle" lineHeight="1.3">
          {title}
        </Text>
      </Flex>
      <Box mt={1.5}>
        <MarkdownContent content={body} fontSize="sm" />
      </Box>
      {onRetry && (
        <Flex mt={2.5}>
          <Button
            variant="solid"
            colorPalette="red"
            fontWeight="medium"
            onClick={onRetry}
            disabled={retrying}
          >
            {retrying ? <ActivitySpinner /> : <LuRotateCw size={13} />}
            {translation(retrying ? "retrying" : "tryAgain")}
          </Button>
        </Flex>
      )}
    </Box>
  );
}

function WarningMessageCard({ message }: { message: ChatMessage }) {
  const translation = useTranslations("ChatMessage");
  const warning = message.meta?.warning;
  if (!warning) return null;
  const title = translation(`warnings.${warning.code}.title`, warning.parameters);
  const body = translation(`warnings.${warning.code}.body`, warning.parameters);
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="orange.muted"
      bg="orange.subtle"
      borderRadius="md"
      px={2.5}
      py={2}
    >
      <Flex align="center" gap={2} color="orange.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text textStyle="panelTitle" lineHeight="1.3">
          {title}
        </Text>
      </Flex>
      <Box mt={1.5}>
        <MarkdownContent content={body} fontSize="sm" />
      </Box>
    </Box>
  );
}

// A failed compaction is an error the person must act on, so it renders as a full card like
// the other server errors rather than a divider label.
function CompactionErrorCard({
  message,
  onRetry,
  retrying = false,
}: {
  message: ChatMessage;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const translation = useTranslations("ChatMessage");
  const errorCode = message.meta?.compactionErrorCode;
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="red.muted"
      bg="red.subtle"
      borderRadius="md"
      px={2.5}
      py={2.5}
    >
      <Flex align="center" gap={2} color="red.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text textStyle="panelTitle" lineHeight="1.3">
          {translation("compactionFailed")}
        </Text>
      </Flex>
      {errorCode ? (
        <Box mt={1.5}>
          <MarkdownContent content={translation(`compactionErrors.${errorCode}`)} fontSize="sm" />
        </Box>
      ) : null}
      {onRetry && (
        <Flex mt={2.5}>
          <Button
            variant="solid"
            colorPalette="red"
            fontWeight="medium"
            px={2.5}
            minH="2rem"
            onClick={onRetry}
            disabled={retrying}
          >
            {retrying ? <ActivitySpinner /> : <LuRotateCw size={13} />}
            {translation(retrying ? "retrying" : "retry")}
          </Button>
        </Flex>
      )}
    </Box>
  );
}

function ToolMessageCard({ message }: ChatMessageProps) {
  return (
    <ToolCall
      name={message.content}
      arguments={message.meta?.arguments as Record<string, unknown> | undefined}
      result={message.meta?.result}
      status={toolStatus(message.meta?.status)}
      permission={message.meta?.permission as ToolPermission | undefined}
      question={message.meta?.question as ToolQuestion | undefined}
      toolCallId={message.meta?.toolCallId as string | undefined}
    />
  );
}

/** A message the session has not taken yet: what it is waiting behind, and what can be done about it. */
export interface QueuedMessageState {
  /** The wait in words, empty while the message is actually being handed over, which is not a wait. */
  status: string;
  /** The wait is a failure to reach the session rather than a place in a line. */
  failed?: boolean;
  /** Remove it before it goes; absent once the session has already taken it, so it cannot be unsent. */
  onDelete?: () => void;
  /** Offered only for the head of a queue that could not be delivered. */
  onRetry?: () => void;
  retryLabel?: string;
}

/** What is under a message you sent: when it was sent, and the small things you can do to it. */
function MessageFooter({
  content,
  sentAt,
  queued,
  reviewId,
  onOpenReview,
}: {
  content: string;
  sentAt: string;
  queued?: QueuedMessageState;
  reviewId?: string;
  onOpenReview?: (reviewId: string) => void;
}) {
  const translation = useTranslations("ChatMessage");
  // A minute is the finest step the wording has, so re-reading the clock more often would change nothing.
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
    },
    [],
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch (caught) {
      // A clipboard write the browser or the person declined, with nothing to recover.
      reportError({ component: "chat-message", operation: "copy a message" }, caught);
    }
  };

  const sent = sentAt ? new Date(sentAt) : null;
  const dated = sent && !Number.isNaN(sent.getTime()) ? sent : null;

  return (
    <Flex
      align="center"
      gap={1}
      color="fg.subtle"
      // The controls lay out from the right, so the nearest is the likeliest wanted and the row grows leftwards.
      justify="flex-end"
    >
      {queued ? (
        queued.status ? (
          <Flex align="center" gap={1.5} color={queued.failed ? "red.fg" : "fg.subtle"} pe={1}>
            <Span display="inline-flex" alignItems="center">
              {queued.failed ? <LuTriangleAlert size={11} /> : <LuClock size={11} />}
            </Span>
            <Text textStyle="fieldLabel" color={queued.failed ? "red.fg" : "fg.subtle"}>
              {queued.status}
            </Text>
          </Flex>
        ) : null
      ) : dated ? (
        // How long ago, worded by the reader's locale and re-read as it ages; the exact instant is on the title.
        <RelativeTime date={dated} textStyle="fieldLabel" pe={1} />
      ) : null}
      {queued?.onRetry && queued.retryLabel && (
        <Button size="2xs" variant="outline" onClick={queued.onRetry}>
          {queued.retryLabel}
        </Button>
      )}
      {content.trim() && (
        <Button size="2xs" variant="ghost" color="fg.subtle" onClick={copy}>
          {copied ? <LuCheck size={11} /> : <LuCopy size={11} />}
          {copied ? translation("copied") : translation("copy")}
        </Button>
      )}
      {reviewId && onOpenReview && (
        <Button size="2xs" variant="ghost" color="fg.subtle" onClick={() => onOpenReview(reviewId)}>
          <LuClipboardCheck size={11} />
          {translation("openReview")}
        </Button>
      )}
      {queued?.onDelete && (
        <Button size="2xs" variant="ghost" colorPalette="red" onClick={queued.onDelete}>
          <LuTrash2 size={11} />
          {translation("deleteQueued")}
        </Button>
      )}
    </Flex>
  );
}

// A message addressed to this session — the person's own, a peer's, or the goal review's — as one card.
export function UserMessageCard({
  message,
  banner = "",
  bannerIcon,
  queued,
  reviewId,
  onOpenReview,
}: {
  message: ChatMessage;
  banner?: string;
  // The mark beside the banner, since who is speaking is read before the words are.
  bannerIcon?: ReactNode;
  queued?: QueuedMessageState;
  reviewId?: string;
  onOpenReview?: (reviewId: string) => void;
}) {
  const translation = useTranslations("ChatMessage");
  const attachments = (message.meta?.attachments as MessageAttachment[] | undefined) ?? [];
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [truncatable, setTruncatable] = useState(false);
  const COLLAPSE_HEIGHT = 200;
  const sourceAuthor = message.meta?.sourceAuthor?.trim();
  const authorBanner = sourceAuthor
    ? sourceAuthor.startsWith("@")
      ? sourceAuthor
      : `@${sourceAuthor}`
    : "";
  const bannerText = authorBanner || banner;

  useLayoutEffect(() => {
    const element = contentRef.current;
    if (!element) return;
    setTruncatable(element.scrollHeight > COLLAPSE_HEIGHT);
  }, [message.content]);

  return (
    <Flex
      className="message-row"
      direction="column"
      alignSelf="flex-end"
      align="flex-end"
      gap={1.5}
      maxW="80%"
    >
      {bannerText && (
        <Flex align="center" gap={1.5} color="fg.muted">
          <ActivityIcon>{bannerIcon ?? <LuMessagesSquare />}</ActivityIcon>
          <Text fontSize="xs" fontWeight="medium">
            {bannerText}
          </Text>
        </Flex>
      )}
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {message.content.trim() && (
        <Box
          ref={contentRef}
          minW={0}
          position="relative"
          overflow="hidden"
          maxH={expanded ? "none" : `${COLLAPSE_HEIGHT}px`}
          // Dashed and a shade back while it is still ours to withdraw, differing only in the ink.
          bg={queued ? "bg.subtle" : "bg.muted"}
          border="1px solid"
          borderStyle={queued ? "dashed" : "solid"}
          borderColor="border"
          px={2.5}
          py={1.5}
          borderRadius="md"
          maxW="100%"
        >
          <MarkdownContent content={message.content} linkGitHubMentions={Boolean(sourceAuthor)} />
          {!expanded && truncatable && (
            <Box
              position="absolute"
              bottom={0}
              left={0}
              right={0}
              h={12}
              pointerEvents="none"
              css={{
                backgroundImage: `linear-gradient(to top, var(--chakra-colors-${queued ? "bg-subtle" : "bg-muted"}), transparent)`,
              }}
            />
          )}
        </Box>
      )}
      {truncatable && (
        <Button
          variant="ghost"
          colorPalette="blue"
          fontWeight="medium"
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? translation("showLess") : translation("showMore")}
        </Button>
      )}
      <MessageFooter
        content={message.content}
        sentAt={message.timestamp}
        queued={queued}
        reviewId={reviewId ?? message.meta?.goalReviewId}
        onOpenReview={onOpenReview}
      />
    </Flex>
  );
}

export const ChatMessageItem = memo(function ChatMessageItem({
  message,
  onRetry,
  retrying = false,
  streaming = false,
  reviewId,
  onOpenReview,
}: ChatMessageProps) {
  const translation = useTranslations("ChatMessage");
  switch (message.role) {
    case "user": {
      return <UserMessageCard message={message} reviewId={reviewId} onOpenReview={onOpenReview} />;
    }

    case "peer": {
      return <UserMessageCard message={message} banner={translation("relayedFromPeerSession")} />;
    }

    case "goal": {
      return (
        <UserMessageCard
          message={message}
          banner={translation("relayedFromGoalReviewAgent")}
          bannerIcon={<LuFlag />}
          onOpenReview={onOpenReview}
        />
      );
    }

    case "assistant": {
      if (!message.contentBlocks) {
        throw new Error("Assistant messages require structured content blocks.");
      }
      const contentBlocks = message.contentBlocks.filter((contentBlock) =>
        contentBlock.content.trim(),
      );
      if (contentBlocks.length === 0) return null;
      return (
        // No horizontal inset, so the prose shares its left edge with the tool-activity lines.
        <Box alignSelf="flex-start">
          <Flex direction="column" gap={3}>
            {contentBlocks.map((contentBlock, contentBlockIndex) => (
              <MarkdownContent
                key={contentBlock.identifier}
                content={contentBlock.content}
                animate={streaming && contentBlockIndex === contentBlocks.length - 1}
              />
            ))}
          </Flex>
        </Box>
      );
    }

    case "thinking":
      return null;

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolMessageCard message={message} />
        </Box>
      );
    }

    case "error":
      // A turn failure is a system event rather than the model's words, so it renders as its own error box.
      return (
        <Box alignSelf="flex-start" w="100%">
          <ErrorMessageCard message={message} onRetry={onRetry} retrying={retrying} />
        </Box>
      );

    case "warning":
      return (
        <Box alignSelf="flex-start" w="100%">
          <WarningMessageCard message={message} />
        </Box>
      );

    case "compaction": {
      // A full-width divider marking where earlier context was summarized away.
      const running = message.meta?.status === "running";
      const failed = message.meta?.status === "failed";
      if (failed) {
        return (
          <Box alignSelf="flex-start" w="100%">
            <CompactionErrorCard message={message} onRetry={onRetry} retrying={retrying} />
          </Box>
        );
      }
      const before = Number(message.meta?.messagesBefore ?? 0);
      const after = Number(message.meta?.messagesAfter ?? 0);
      return (
        <Box alignSelf="stretch" w="100%">
          <Flex align="center" gap={3} py={1} color="fg.subtle">
            <Separator flex={1} />
            <Flex
              align="center"
              gap={1.5}
              flexShrink={0}
              color={running ? "blue.fg" : undefined}
              title={
                running || !before ? undefined : translation("compactedTooltip", { before, after })
              }
            >
              <ActivityIcon>{running ? <ActivitySpinner /> : <LuFoldVertical />}</ActivityIcon>
              <Box>
                <Text
                  textStyle="fieldLabel"
                  className={running ? "running-title-shimmer" : undefined}
                >
                  {running ? translation("compactingContext") : translation("contextCompacted")}
                </Text>
              </Box>
            </Flex>
            <Separator flex={1} />
          </Flex>
        </Box>
      );
    }

    default:
      return null;
  }
});

interface ChatToolGroupProps {
  messages: ChatMessage[];
  live?: boolean;
}

export const ChatToolGroup = memo(function ChatToolGroup({
  messages,
  live = false,
}: ChatToolGroupProps) {
  // Map the persisted tool-call messages to the shape the shared group renders.
  const tools: ToolEvent[] = messages.map((message) => ({
    name: message.content,
    arguments: message.meta?.arguments as Record<string, unknown> | undefined,
    argumentsComplete: message.meta?.argumentsComplete,
    toolCallId: String(message.meta?.toolCallId ?? ""),
    result: message.meta?.result,
    status: toolStatus(message.meta?.status),
    permission: message.meta?.permission as ToolPermission | undefined,
    question: message.meta?.question as ToolQuestion | undefined,
  }));
  const readyTools = tools.filter(toolCallReady);
  if (readyTools.length === 0) return null;
  return <ToolGroup tools={readyTools} live={live} />;
});
