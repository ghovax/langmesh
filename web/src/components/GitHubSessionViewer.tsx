"use client";

import { Box, Flex, Link, Spinner, Text, VStack } from "@chakra-ui/react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  LuCircleHelp,
  LuExternalLink,
  LuGitPullRequest,
  LuMessageSquare,
  LuMoon,
  LuSun,
} from "react-icons/lu";
import { ChatInput } from "./ChatInput";
import { ChatMessageItem, ChatToolGroup } from "./ChatMessage";
import { PanelEmptyState, TOP_BAR_HEIGHT } from "./ui/Panel";
import { ToolbarAction } from "./ui/Toolbar";
import type { ChatMessage, ChatTask, TokenUsage } from "@/lib/use-chat";
import { timelineItems } from "@/lib/chat-timeline";

interface ViewerSnapshot {
  session_id: string;
  agent: string;
  repository: string;
  number: number;
  kind: "issue" | "pull";
  title: string;
  source_url: string;
  provider: string;
  model: string;
  model_name: string;
  permission_mode: "ask" | "automatic" | "allow";
  sandbox_enforce: "required" | "preferred" | "off";
  sandbox_backend: string;
  status: "working" | "queued" | "completed" | "failed";
  tasks: ChatTask[];
  token_usage: Record<string, unknown>;
  messages: ChatMessage[];
}

function numberValue(values: Record<string, unknown>, name: string): number {
  const value = values[name];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function tokenUsageFromSnapshot(snapshot: ViewerSnapshot | null): TokenUsage | null {
  if (!snapshot) return null;
  const values = snapshot.token_usage ?? {};
  const contextInputTokens = numberValue(values, "context_input_tokens");
  const contextOutputTokens = numberValue(values, "context_output_tokens");
  if (contextInputTokens + contextOutputTokens <= 0) return null;
  const cachePrefixReusable = values.cache_prefix_reusable;
  return {
    inputTokens: numberValue(values, "input_tokens"),
    outputTokens: numberValue(values, "output_tokens"),
    totalTokens: numberValue(values, "total_tokens"),
    cacheReadTokens: numberValue(values, "cache_read_tokens"),
    cacheWriteTokens: numberValue(values, "cache_write_tokens"),
    cacheReusablePrefixTokens: numberValue(values, "reusable_prefix_tokens"),
    reasoningTokens: numberValue(values, "reasoning_tokens"),
    modelCalls: numberValue(values, "model_calls"),
    contextTokens: contextInputTokens + contextOutputTokens,
    contextInputTokens,
    contextOutputTokens,
    contextWindow: numberValue(values, "context_window"),
    contextWindowEstimated: values.context_window_estimated === true,
    contextCacheReadTokens: numberValue(values, "context_cache_read_tokens"),
    contextCacheWriteTokens: numberValue(values, "context_cache_write_tokens"),
    reusablePrefixTokens: numberValue(values, "latest_reusable_prefix_tokens"),
    cachePrefixReusable: typeof cachePrefixReusable === "boolean" ? cachePrefixReusable : null,
    divergence: null,
  };
}

function sourceIcon(kind: ViewerSnapshot["kind"] | undefined) {
  if (kind === "pull") return <LuGitPullRequest size={14} />;
  if (kind === "issue") return <LuCircleHelp size={14} />;
  return <LuMessageSquare size={14} />;
}

function ViewerThemeAction() {
  const [colorMode, setColorMode] = useState<"light" | "dark">(() => {
    if (typeof window === "undefined") return "light";
    const stored = window.localStorage.getItem("langmesh-viewer-color-mode");
    return stored === "dark" || stored === "light"
      ? stored
      : window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
  });

  useEffect(() => {
    document.documentElement.classList.remove("light", "dark");
    document.documentElement.classList.add(colorMode);
    document.documentElement.style.colorScheme = colorMode;
  }, [colorMode]);

  const toggle = useCallback(() => {
    setColorMode((current) => {
      const next = current === "dark" ? "light" : "dark";
      window.localStorage.setItem("langmesh-viewer-color-mode", next);
      return next;
    });
  }, []);

  return (
    <ToolbarAction
      label={colorMode === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      icon={colorMode === "dark" ? <LuSun size={14} /> : <LuMoon size={14} />}
      onClick={toggle}
    />
  );
}

const VIEWER_AGENT = {
  id: "langmesh",
  name: "LangMesh",
  title: "LangMesh",
  description: "GitHub mention agent",
};

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
  const provider = snapshot?.provider || "";
  const model = snapshot?.model || "";
  const modelName = snapshot?.model_name || model;
  const modelIdentifier = provider && model ? `${provider}/${model}` : "";
  const models = modelIdentifier
    ? [{ id: modelIdentifier, name: modelName, provider, available: true }]
    : [];
  const providers = provider
    ? [
        {
          id: provider,
          name: provider === "chatgpt" ? "ChatGPT" : provider,
          openai_compatible: false,
          credential_id: provider,
        },
      ]
    : [];
  const selectedAgent = snapshot?.agent || VIEWER_AGENT.id;
  const permissionMode = snapshot?.permission_mode || "automatic";
  const sandboxEnforce = snapshot?.sandbox_enforce || "required";
  const sandboxBackend = snapshot?.sandbox_backend || "Render";
  const tokenUsage = tokenUsageFromSnapshot(snapshot);

  return (
    <Flex h="100dvh" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        <Flex align="center" gap={2} pl={4} pr={2} h={TOP_BAR_HEIGHT} flexShrink={0} minW={0}>
          <Box color="fg.muted" flexShrink={0}>
            {sourceIcon(snapshot?.kind)}
          </Box>
          <Text textStyle="panelTitle" fontWeight="medium" whiteSpace="nowrap" truncate flex={1}>
            {snapshot?.title || issueLabel}
          </Text>
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
          <ViewerThemeAction />
        </Flex>

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
              <PanelEmptyState icon={<Spinner boxSize="8" borderWidth="2px" />} />
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
              agents={[VIEWER_AGENT]}
              selectedAgent={selectedAgent}
              onAgentChange={() => {}}
              models={models}
              modelProviders={providers}
              agentModel={modelIdentifier}
              permissionMode={permissionMode}
              sandboxEnforce={sandboxEnforce}
              sandboxBackend={sandboxBackend}
              tokenUsage={tokenUsage}
              tasks={snapshot?.tasks ?? []}
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
