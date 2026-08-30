"use client";

import { Box, Flex, Link, Text, VStack } from "@chakra-ui/react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { LuCircleHelp, LuExternalLink, LuGitPullRequest, LuMessageSquare } from "react-icons/lu";
import { ChatInput } from "./ChatInput";
import { ChatMessageItem, ChatToolGroup } from "./ChatMessage";
import { ActivitySpinner } from "./ui/ActivityIcon";
import { ColorModeButton } from "./ui/ColorMode";
import { PanelEmptyState, PanelHeader } from "./ui/Panel";
import type { ChatMessage } from "@/lib/use-chat";
import { timelineItems } from "@/lib/chat-timeline";

interface ViewerSnapshot {
  session_id: string;
  repository: string;
  number: number;
  kind: "issue" | "pull";
  title: string;
  source_url: string;
  provider: string;
  model: string;
  status: "working" | "queued" | "completed" | "failed";
  messages: ChatMessage[];
}

function sourceIcon(kind: ViewerSnapshot["kind"] | undefined) {
  if (kind === "pull") return <LuGitPullRequest size={14} />;
  if (kind === "issue") return <LuCircleHelp size={14} />;
  return <LuMessageSquare size={14} />;
}

function ViewerContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [snapshot, setSnapshot] = useState<ViewerSnapshot | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return;
    const stream = new EventSource(
      `/github/session?token=${encodeURIComponent(token)}&mode=stream`,
    );
    stream.onmessage = (event) => {
      try {
        setSnapshot(JSON.parse(event.data) as ViewerSnapshot);
        setError("");
      } catch {
        setError("The session returned an invalid update.");
      }
    };
    stream.onerror = () => setError("The session viewer is temporarily unavailable.");
    return () => stream.close();
  }, [token]);

  const stop = useCallback(async () => {
    if (!token) return;
    try {
      const response = await fetch(`/github/session?token=${encodeURIComponent(token)}&mode=stop`, {
        method: "POST",
      });
      if (!response.ok) setError("The session could not be stopped.");
    } catch {
      setError("The session could not be stopped.");
    }
  }, [token]);

  const timeline = useMemo(() => timelineItems(snapshot?.messages ?? []), [snapshot?.messages]);
  const issueLabel = snapshot ? `${snapshot.repository}#${snapshot.number}` : "GitHub session";
  const visibleError = error || (!token ? "This session link is missing its access token." : "");
  const waitingForSession =
    !snapshot || (timeline.length === 0 && !["completed", "failed"].includes(snapshot.status));

  return (
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        <PanelHeader icon={sourceIcon(snapshot?.kind)} title={snapshot?.title || issueLabel}>
          {snapshot?.source_url ? (
            <Link
              href={snapshot.source_url}
              target="_blank"
              rel="noreferrer"
              display="flex"
              alignItems="center"
              gap={1}
              color="fg.muted"
              fontSize="xs"
              whiteSpace="nowrap"
            >
              {issueLabel}
              <LuExternalLink size={12} />
            </Link>
          ) : null}
          <ColorModeButton />
        </PanelHeader>

        <Box position="relative" flex={1} minH={0} display="flex" flexDirection="column">
          <Box
            flex={1}
            minH={0}
            display="flex"
            flexDirection="column-reverse"
            overflowY="auto"
            px={4}
            py={3}
            style={{ scrollbarGutter: "stable both-edges" }}
          >
            {visibleError ? (
              <Text color="red.fg" textAlign="center">
                {visibleError}
              </Text>
            ) : waitingForSession ? (
              <PanelEmptyState icon={<ActivitySpinner />} title="Loading session" />
            ) : snapshot && timeline.length > 0 ? (
              <VStack gap={2.5} align="stretch" w="full" maxW="80rem" mx="auto">
                {timeline.map((item) =>
                  item.kind === "tool_group" ? (
                    <ChatToolGroup key={item.id} messages={item.messages} />
                  ) : (
                    <ChatMessageItem key={item.message.id} message={item.message} />
                  ),
                )}
              </VStack>
            ) : (
              <PanelEmptyState icon={<LuMessageSquare />} title="No visible messages" />
            )}
          </Box>
        </Box>

        <Box
          px={4}
          pb="var(--safe-bottom, 0px)"
          overflowY="hidden"
          flexShrink={0}
          style={{ scrollbarGutter: "stable both-edges" }}
        >
          <Box w="full" maxW="80rem" mx="auto">
            <ChatInput
              onSend={() => {}}
              onAbort={stop}
              isStreaming={snapshot?.status === "working"}
              readOnly
              sessionId={snapshot?.session_id ?? null}
              directoryAvailable={false}
              agents={[]}
              selectedAgent=""
              onAgentChange={() => {}}
              models={[]}
              modelProviders={[]}
              onAgentModelChange={() => {}}
            />
          </Box>
        </Box>
      </Flex>
    </Flex>
  );
}

export function GitHubSessionViewer() {
  return <ViewerContent />;
}
