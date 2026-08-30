"use client";

import { Box, Button, Flex, Input, Text } from "@chakra-ui/react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ChatMessageItem } from "./ChatMessage";
import { TOP_BAR_HEIGHT } from "./ui/Panel";
import type { ChatMessage } from "@/lib/use-chat";

interface ViewerSnapshot {
  repository: string;
  number: number;
  kind: "issue" | "pull";
  title: string;
  provider: string;
  model: string;
  status: "working" | "queued" | "completed" | "failed";
  messages: ChatMessage[];
}

function statusLabel(status: ViewerSnapshot["status"]): string {
  switch (status) {
    case "working":
      return "Working";
    case "queued":
      return "Queued";
    case "failed":
      return "Failed";
    default:
      return "Completed";
  }
}

function ViewerContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [snapshot, setSnapshot] = useState<ViewerSnapshot | null>(null);
  const [error, setError] = useState("");
  const [stopRequested, setStopRequested] = useState(false);

  useEffect(() => {
    if (!token) return;
    const stream = new EventSource(`/github/session-stream/${encodeURIComponent(token)}`);
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
    setStopRequested(true);
    try {
      const response = await fetch(`/github/session-stop/${encodeURIComponent(token)}`, {
        method: "POST",
      });
      if (!response.ok) setError("The session could not be stopped.");
    } catch {
      setError("The session could not be stopped.");
    } finally {
      setStopRequested(false);
    }
  }, [token]);

  const issueLabel = snapshot ? `${snapshot.repository}#${snapshot.number}` : "GitHub session";
  const visibleError = error || (!token ? "This session link is missing its access token." : "");

  return (
    <Flex h="100dvh" direction="column" bg="bg" color="fg">
      <Flex
        align="center"
        gap={3}
        px={4}
        h={TOP_BAR_HEIGHT}
        flexShrink={0}
        borderBottom="1px solid"
        borderColor="border"
      >
        <Text textStyle="panelTitle" fontWeight="medium" whiteSpace="nowrap">
          {snapshot?.title || issueLabel}
        </Text>
        <Text color="fg.muted" fontSize="xs" whiteSpace="nowrap">
          {issueLabel}
        </Text>
        <Box flex={1} />
        <Text color="fg.muted" fontSize="xs" whiteSpace="nowrap">
          {snapshot?.model || ""}
        </Text>
        <Text
          fontSize="xs"
          fontWeight="medium"
          color={snapshot?.status === "failed" ? "red.fg" : "blue.fg"}
        >
          {snapshot ? statusLabel(snapshot.status) : "Loading"}
        </Text>
        {snapshot?.status === "working" && (
          <Button
            size="xs"
            variant="outline"
            colorPalette="red"
            onClick={stop}
            disabled={stopRequested}
          >
            {stopRequested ? "Stopping" : "Stop"}
          </Button>
        )}
      </Flex>

      <Box flex={1} minH={0} overflowY="auto" px={4} py={6}>
        <Box w="full" maxW="80rem" mx="auto">
          {visibleError ? (
            <Text color="red.fg" textAlign="center">
              {visibleError}
            </Text>
          ) : snapshot && snapshot.messages.length > 0 ? (
            <Flex direction="column" gap={6}>
              {snapshot.messages.map((message) => (
                <ChatMessageItem key={message.id} message={message} />
              ))}
            </Flex>
          ) : (
            <Text color="fg.muted" textAlign="center">
              Waiting for the first session update.
            </Text>
          )}
        </Box>
      </Box>

      <Flex
        align="center"
        gap={2}
        px={4}
        py={3}
        borderTop="1px solid"
        borderColor="border"
        opacity={0.7}
      >
        <Input disabled placeholder="Read-only session" aria-label="Read-only session input" />
        <Button disabled>Send</Button>
      </Flex>
    </Flex>
  );
}

export function GitHubSessionViewer() {
  return <ViewerContent />;
}
