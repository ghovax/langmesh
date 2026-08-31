"use client";

import {
  Box,
  Button,
  Flex,
  IconButton,
  Input,
  Separator,
  Span,
  Spinner,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  LuArrowUp,
  LuCoins,
  LuFoldVertical,
  LuListChecks,
  LuMic,
  LuMicOff,
  LuPaperclip,
  LuSquare,
} from "react-icons/lu";
import {
  fetchChatGPTAuthStatus,
  fetchDictationStatus,
  type DictationState,
  fetchMessageHistory,
  referenceAttachment,
  saveMessageHistory,
  subscribeEvents,
  uploadFile,
  type Attachment,
  type ChatGPTUsage,
  type ModelOption,
  type PermissionMode,
  type ProviderOption,
  type SandboxEnforce,
} from "@/lib/api";
import { DictationRecordingError, startDictation, type Dictation } from "@/lib/dictation";
import { toaster } from "./ui/Toaster";
import { ChatGPTUsageMeters } from "./ChatgptUsageMeters";
import { AgentSelectControl, PermissionModeControl, SandboxToggleControl } from "./SessionControls";
import { isTauri } from "@/lib/tauri";
import { pickDesktopFilePaths, watchDesktopFileDrop } from "@/lib/desktop-files";
import { AttachmentChip } from "./AttachmentChips";
import { Tooltip } from "./ui/Tooltip";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { ModelSelect, modelSupportsVision } from "./ModelSelect";
// SettingsDialog moved to ChatPanel top bar
import type { ChatTask, TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./ui/Display";
import { richTags } from "@/lib/i18n/rich-tags";
import { reportError } from "@/lib/faults";
import { errorMessage } from "@/lib/errors";
import {
  hideHorizontalScrollbar,
  FADE_INLINE,
  fadeOverlayInline,
  useScrollInlineFade,
} from "@/lib/scroll-fade";

interface ChatInputProps {
  // Returns the session id when the send created one, which the composer ignores as the caller's business.
  onSend: (text: string, dataParts?: Record<string, unknown>[]) => void | Promise<void | string>;
  onAbort: () => void | Promise<void>;
  isStreaming: boolean;
  // The connection is gone. Nothing can be sent, and saying why is the point.
  disabled?: boolean;
  // The transcript is being displayed without allowing edits, while a running turn may still be stopped.
  readOnly?: boolean;
  // A decision prompt is open, which closes the composer for a different reason than a lost network.
  awaitingDecision?: boolean;
  sessionId?: string | null;
  initialDraft?: string;
  onDraftChange?: (draft: string) => void;
  workingDirectory?: string;
  // Optimistically available while validation runs, and false only after the directory is rejected.
  directoryAvailable?: boolean;
  agents: { id: string; name: string; title?: string; description?: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  models: ModelOption[];
  modelProviders: ProviderOption[];
  recentModels?: { id: string; name: string; provider: string }[];
  // The active agent's configured model, driving the chip and the attachment gates for every session running that agent.
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  // Re-fetches the model catalog through the daemon, the retry path for a failed initial load.
  onRetryModels?: () => void | Promise<void>;
  // The session's permission mode and its change handler, surfaced here so it is adjustable inline.
  permissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  // Whether this machine confines what tools may touch, beside the permission mode because they answer one question together.
  sandboxEnforce?: SandboxEnforce;
  sandboxBackend?: string;
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void;
  // Running token totals for the session, null until the first turn reports usage.
  tokenUsage?: TokenUsage | null;
  // Provider-reported subscription usage supplied by a hosted session viewer.
  subscriptionUsage?: ChatGPTUsage | null;
  // The tracked task list, updated by the model's set_tasks/update_tasks calls.
  tasks?: ChatTask[];
  // Compact the conversation now, offered whenever there is one.
  onCompact?: () => void;
  // True while a compaction pass is running, so the control shows progress rather than inviting another click.
  isCompacting?: boolean;
  // The share of the window at which the server reclaims on its own; the manual button appears at half of it.
}

// A filling ring for how full the context window is, shifting colour as it approaches the limit.
function ContextFillRing({
  fraction,
  tone = "context",
}: {
  fraction: number;
  tone?: "context" | "tasks";
}) {
  const clamped = Math.max(0, Math.min(1, fraction));
  const radius = 5.5;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - clamped);
  const stroke =
    tone === "tasks"
      ? "var(--chakra-colors-green-solid)"
      : clamped >= 0.9
        ? "var(--chakra-colors-red-solid)"
        : clamped >= 0.75
          ? "var(--chakra-colors-orange-solid)"
          : "var(--chakra-colors-blue-solid)";
  return (
    <Box
      w="13px"
      h="13px"
      flexShrink={0}
      display="flex"
      alignItems="center"
      justifyContent="center"
    >
      <svg width="13" height="13" viewBox="0 0 14 14">
        <circle
          cx="7"
          cy="7"
          r={radius}
          fill="none"
          stroke="var(--chakra-colors-bg-muted)"
          strokeWidth="2"
        />
        <circle
          cx="7"
          cy="7"
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 7 7)"
        />
      </svg>
    </Box>
  );
}

// The plan usage for the token view, supplied by a hosted viewer or fetched from the local daemon.
function useChatGPTUsage(
  agentModel: string | undefined,
  isStreaming: boolean,
  suppliedUsage?: ChatGPTUsage | null,
): ChatGPTUsage | null {
  const isChatGPT = !!agentModel && agentModel.startsWith("chatgpt/");
  const [usage, setUsage] = useState<ChatGPTUsage | null>(null);

  // Fetch whenever the model is chatgpt and nothing is streaming, which covers both mount and each turn's end.
  useEffect(() => {
    if (!isChatGPT || isStreaming || suppliedUsage !== undefined) return;
    let cancelled = false;
    fetchChatGPTAuthStatus()
      .then((status) => {
        if (!cancelled) setUsage(status?.usage ?? null);
      })
      .catch((caught) =>
        reportError({ component: "chat-input", operation: "read the ChatGPT plan usage" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [isChatGPT, isStreaming, suppliedUsage]);

  return isChatGPT ? (suppliedUsage ?? usage) : null;
}

function cacheCoverage(readTokens: number, reusableTokens: number): string {
  const read = readTokens.toLocaleString();
  if (reusableTokens <= 0) return read;
  const percent = Math.min(100, Math.round((readTokens / reusableTokens) * 100));
  return `${read} / ${reusableTokens.toLocaleString()} (${percent}%)`;
}

// The context-usage chip: a fill ring and percent, then the current context size against the model's window.
function ContextUsageChip({
  tokenUsage,
  chatgptUsage,
}: {
  tokenUsage?: TokenUsage | null;
  chatgptUsage?: ChatGPTUsage | null;
}) {
  const translation = useTranslations("ChatInput");
  if (!tokenUsage || tokenUsage.contextTokens <= 0) return null;
  const hasContext = tokenUsage.contextWindow > 0;
  const contextFraction = hasContext ? tokenUsage.contextTokens / tokenUsage.contextWindow : 0;
  const contextPercent = Math.min(100, Math.round(contextFraction * 100));
  const tooltipContent = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("sessionTotals")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("input")}>
          <Text>{tokenUsage.inputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("output")}>
          <Text>{tokenUsage.outputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("total")}>
          <Text>{tokenUsage.totalTokens.toLocaleString()}</Text>
        </InlineField>
        {/* Always shown because a zero provider cache read is still a useful measurement. */}
        <InlineField label={translation("cacheReads")}>
          <Text>
            {cacheCoverage(tokenUsage.cacheReadTokens, tokenUsage.cacheReusablePrefixTokens)}
          </Text>
        </InlineField>
        <InlineField label={translation("cacheWrites")}>
          <Text>{tokenUsage.cacheWriteTokens.toLocaleString()}</Text>
        </InlineField>
        {tokenUsage.reasoningTokens > 0 && (
          <InlineField label={translation("reasoning")}>
            <Text>{tokenUsage.reasoningTokens.toLocaleString()}</Text>
          </InlineField>
        )}
        <InlineField label={translation("modelCalls")}>
          <Text>{tokenUsage.modelCalls}</Text>
        </InlineField>
      </Flex>
      <Separator my={2} />
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("usageThisTurn")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("input")}>
          <Text>{tokenUsage.contextInputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("output")}>
          <Text>{tokenUsage.contextOutputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("cacheReads")}>
          <Text>
            {cacheCoverage(tokenUsage.contextCacheReadTokens, tokenUsage.reusablePrefixTokens)}
          </Text>
        </InlineField>
        <InlineField label={translation("cacheWrites")}>
          <Text>{tokenUsage.contextCacheWriteTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("cachePrefix")}>
          <Text>
            {translation(
              tokenUsage.cachePrefixReusable == null
                ? "cachePrefixUnknown"
                : tokenUsage.cachePrefixReusable
                  ? "cachePrefixIntact"
                  : "cachePrefixMoved",
            )}
          </Text>
        </InlineField>
        {hasContext && (
          <InlineField label={translation("window")}>
            <Text>
              {tokenUsage.contextWindowEstimated
                ? translation("estimatedWindow", {
                    value: tokenUsage.contextWindow.toLocaleString(),
                  })
                : tokenUsage.contextWindow.toLocaleString()}
            </Text>
          </InlineField>
        )}
      </Flex>
      {chatgptUsage && chatgptUsage.windows.length > 0 && (
        <>
          <Separator my={2} />
          <Box w="210px" whiteSpace="normal">
            <ChatGPTUsageMeters usage={chatgptUsage} />
          </Box>
        </>
      )}
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      rich
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Button
        type="button"
        aria-label={translation("usageThisTurn")}
        variant="outline"
        alignItems="center"
        gap={1.5}
        // The house control height rather than a number, so this chip matches the buttons beside it on a coarse pointer.
        h="var(--control-height)"
        px={2}
        borderRadius="md"
        border="1px solid"
        borderColor="border"
        bg="bg"
        color="fg.subtle"
        cursor="pointer"
        pointerEvents="auto"
        flexShrink={0}
      >
        {hasContext && (
          <>
            <ContextFillRing fraction={contextFraction} />
            <Text textStyle="fieldLabel" whiteSpace="nowrap">
              {contextPercent}%
            </Text>
            <Separator orientation="vertical" h={3.5} flexShrink={0} />
          </>
        )}
        {/* The tokens icon is the chip's fallback: it stays when the numbers are shed, so the chip is never an empty box. */}
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuCoins size={14} />
        </Box>
        <Text textStyle="fieldLabel" whiteSpace="nowrap">
          {tokenUsage.contextTokens.toLocaleString()}
          {hasContext
            ? ` / ${tokenUsage.contextWindowEstimated ? "~" : ""}${tokenUsage.contextWindow.toLocaleString()}`
            : ""}
        </Text>
      </Button>
    </Tooltip>
  );
}

// The tasks chip: a completion ring beside the icon, and the list with each one's status in the tooltip.
function TasksChip({ tasks }: { tasks: ChatTask[] }) {
  const translation = useTranslations("ChatInput");
  const active = tasks.filter((task) => task.status !== "completed");
  const completed = tasks.length - active.length;
  if (tasks.length === 0) return null;
  const completion = completed / tasks.length;
  const tooltipContent = (
    <Box whiteSpace="nowrap">
      <Flex align="baseline" gap={2} mb={1}>
        <Text fontWeight="semibold" color="fg">
          {translation("tasks")}
        </Text>
        <Text color="fg.subtle">
          {translation("tasksProgress", { total: tasks.length, completed })}
        </Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1} maxH="60vh" overflowY="auto">
        {tasks.map((task) => (
          <Flex
            key={task.identifier}
            align="center"
            gap={2}
            minW="220px"
            opacity={task.status === "completed" ? 0.6 : 1}
          >
            <Box
              flexShrink={0}
              w="8px"
              h="8px"
              borderRadius="full"
              bg={
                task.status === "completed"
                  ? "green.500"
                  : task.status === "in_progress"
                    ? "blue.500"
                    : task.status === "blocked"
                      ? "orange.500"
                      : "gray.400"
              }
            />
            <Text
              textStyle="bodySm"
              color={task.status === "completed" ? "fg.subtle" : "fg"}
              css={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {task.title || task.description}
            </Text>
          </Flex>
        ))}
      </Flex>
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      rich
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Flex
        align="center"
        gap={1.5}
        h="var(--control-height)"
        px={2}
        borderRadius="md"
        border="1px solid"
        borderColor="border"
        bg="bg"
        color="fg.subtle"
        flexShrink={0}
      >
        <ContextFillRing fraction={completion} tone="tasks" />
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuListChecks size={14} />
        </Box>
      </Flex>
    </Tooltip>
  );
}

// Lucide glyphs fill their 24-unit box by different amounts, so a shared box size draws them at different heights;
// `draw` is the box that makes this one's ink match the icon-only buttons next to it.
function ComposerIcon({ draw, children }: { draw: number; children: ReactNode }) {
  return (
    <Box
      display="flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
      css={{ "& svg": { width: `${draw}px`, height: `${draw}px` } }}
    >
      {children}
    </Box>
  );
}

// Send and Stop share one control so a narrow screen can square them to the attach button beside them.
function ComposerActionButton({
  colorPalette,
  onClick,
  busy,
  disabled,
  label,
  tooltip,
  icon,
}: {
  colorPalette: "blue" | "red";
  onClick: () => void;
  busy: boolean;
  disabled: boolean;
  label: string;
  tooltip?: string;
  icon: ReactNode;
}) {
  return (
    <Tooltip content={tooltip ?? label} openDelay={200} positioning={{ placement: "top" }}>
      <Button
        data-composer-action=""
        type="button"
        onClick={onClick}
        size="sm"
        colorPalette={colorPalette}
        variant="solid"
        disabled={disabled}
        aria-label={label}
      >
        {busy ? <Spinner boxSize="18px" borderWidth="2px" /> : icon}
        <Span data-composer-action-label="">{label}</Span>
      </Button>
    </Tooltip>
  );
}

function StopTurnButton({
  onAbort,
  isCompacting,
}: {
  onAbort: () => void | Promise<void>;
  isCompacting: boolean;
}) {
  const translation = useTranslations("ChatInput");
  const [pending, setPending] = useState(false);

  async function stop() {
    setPending(true);
    try {
      await onAbort();
    } catch {
      // The cancel call itself failed; the turn is untouched, so the control releases at once.
      setPending(false);
      return;
    }
    // Last resort: a stop that never reaches the stream must not wedge the button forever.
    window.setTimeout(() => setPending(false), 15_000);
  }

  const label = pending ? translation("stopping") : translation("stop");
  return (
    <ComposerActionButton
      colorPalette="red"
      onClick={() => void stop()}
      busy={pending}
      disabled={pending || isCompacting}
      label={label}
      tooltip={isCompacting ? translation("stopUnavailableWhileCompacting") : label}
      icon={
        <ComposerIcon draw={18}>
          <LuSquare />
        </ComposerIcon>
      }
    />
  );
}

export function ChatInput({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  readOnly = false,
  sessionId,
  initialDraft = "",
  onDraftChange,
  workingDirectory,
  awaitingDecision,
  directoryAvailable = false,
  agents,
  selectedAgent,
  onAgentChange,
  models,
  modelProviders,
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  onRetryModels,
  permissionMode = "ask",
  onPermissionModeChange,
  sandboxEnforce = "required",
  sandboxBackend = "",
  onSandboxEnforceChange,
  tokenUsage,
  subscriptionUsage,
  tasks = [],
  onCompact,
  isCompacting = false,
}: ChatInputProps) {
  const translation = useTranslations("ChatInput");
  const chatgptUsage = useChatGPTUsage(agentModel, isStreaming, subscriptionUsage);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dropZoneRef = useRef<HTMLDivElement>(null);
  // Closed only when there is nothing to talk to; an open decision parks the session rather than removing it.
  const composerClosed = disabled;
  const [inputValue, setInputValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [messageHistory, setMessageHistory] = useState<string[]>([]);
  const draftInputRef = useRef("");
  const persistedDraftKeyRef = useRef("");
  const latestInputValueRef = useRef("");
  const [sendPending, setSendPending] = useState(false);
  const [compactConfirmOpen, setCompactConfirmOpen] = useState(false);
  const {
    containerRef: selectorsScrollRef,
    onScroll: onSelectorsScroll,
    hiddenStart: selectorsHiddenStart,
    hiddenEnd: selectorsHiddenEnd,
  } = useScrollInlineFade();
  // Dictation is absent rather than disabled until it is turned on, and `recording` holds the take a toggle can stop.
  const [dictationEnabled, setDictationEnabled] = useState(false);
  const [dictationState, setDictationState] = useState<DictationState>("idle");
  const [recording, setRecording] = useState<Dictation | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  // The attach affordance is gated on the model's vision capability, though unknown models are not blocked.
  const effectiveModelId = agentModel;
  const visionSupported = modelSupportsVision(models, effectiveModelId);
  const attachmentTooltipContent = (
    <Box w="420px" maxW="calc(100vw - 32px)">
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("fileAttachments")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("images")}>
          <Text>
            {visionSupported ? translation("imagesSupported") : translation("imagesUnsupported")}
          </Text>
        </InlineField>
      </Flex>
    </Box>
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const draftKey = sessionId || "__new__";

  useEffect(() => {
    latestInputValueRef.current = inputValue;
  }, [inputValue]);

  // Seed the composer from the session's stored draft, accepting one that lands after the composer mounted.
  useEffect(() => {
    const isNewSession = persistedDraftKeyRef.current !== draftKey;
    if (!isNewSession && !(initialDraft && latestInputValueRef.current === "")) return;
    persistedDraftKeyRef.current = draftKey;
    const restoredDraft = sessionId ? initialDraft : "";
    setInputValue(restoredDraft);
    draftInputRef.current = restoredDraft;
    setHistoryIndex(-1);
  }, [draftKey, initialDraft, sessionId]);

  useEffect(() => {
    if (!sessionId || !onDraftChange) return;
    const timer = window.setTimeout(() => {
      onDraftChange(latestInputValueRef.current);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [inputValue, onDraftChange, sessionId]);

  useEffect(() => {
    if (!sessionId || !onDraftChange) return;
    return () => {
      onDraftChange(latestInputValueRef.current);
    };
  }, [onDraftChange, sessionId]);

  // Fetch message history when the working directory changes.
  useEffect(() => {
    let cancelled = false;
    if (!workingDirectory) {
      queueMicrotask(() => {
        if (!cancelled) setMessageHistory([]);
      });
      return () => {
        cancelled = true;
      };
    }
    fetchMessageHistory(workingDirectory)
      .then((history) => {
        if (!cancelled) setMessageHistory(history);
      })
      .catch((caught) =>
        reportError({ component: "chat-input", operation: "read the message history" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [workingDirectory]);

  // The browser only ever hands us file bytes, so a sandboxed or remote build uploads them.
  async function handleFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    if (readOnly || selected.length === 0) return;
    setUploadingCount((current) => current + selected.length);
    for (const file of selected) {
      try {
        const uploaded = await uploadFile(file);
        setAttachments((current) => [...current, uploaded]);
      } catch {
        // Send stays disabled only while uploads are in flight; a failed upload simply never becomes an attachment.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  // On the desktop the file is referenced by its real path in place, valid only when the server is this machine.
  async function attachByPaths(paths: string[]) {
    if (readOnly || paths.length === 0) return;
    setUploadingCount((current) => current + paths.length);
    for (const path of paths) {
      try {
        const attachment = await referenceAttachment(path);
        setAttachments((current) => [...current, attachment]);
      } catch {
        // A path that no longer exists (or a race with a rename) simply does not attach.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  // Whether the microphone is offered and what its model is doing, asked with `prepare` so the weights warm up.
  useEffect(() => {
    if (readOnly) return;
    let cancelled = false;
    let timer: number | undefined;
    const read = (prepare: boolean) => {
      fetchDictationStatus(prepare)
        .then((status) => {
          if (cancelled) return;
          setDictationEnabled(status.enabled);
          setDictationState(status.state);
          if (status.enabled && status.state === "loading") {
            timer = window.setTimeout(() => read(false), 1000);
          }
        })
        .catch((caught) =>
          reportError({ component: "chat-input", operation: "read the dictation status" }, caught),
        );
    };
    read(true);
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "settings_changed") read(true);
    });
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [readOnly]);

  // Stop the microphone if the composer goes away mid-recording, so the machine never keeps listening.
  const recordingRef = useRef<Dictation | null>(null);
  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);
  useEffect(() => () => recordingRef.current?.cancel(), []);

  // The dictation toggle: press to start and again to stop, appending the text to what is already typed.
  async function handleDictationClick() {
    if (transcribing) return;
    const active = recording;
    if (active) {
      setRecording(null);
      setTranscribing(true);
      try {
        const spoken = (await active.stop()).trim();
        if (!spoken) return;
        setInputValue((current) => {
          const next = current.trim() ? `${current.trimEnd()} ${spoken}` : spoken;
          latestInputValueRef.current = next;
          return next;
        });
        inputRef.current?.focus();
      } catch (caught) {
        toaster.create({
          type: "error",
          title: translation("dictationFailed"),
          description: dictationReason(caught),
          closable: true,
        });
      } finally {
        setTranscribing(false);
      }
      return;
    }
    try {
      setRecording(await startDictation());
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("dictationFailed"),
        description: dictationReason(caught),
        closable: true,
      });
    }
  }

  // On the desktop open the native picker and reference files by path; otherwise fall back to the web input.
  /** What to tell somebody about a dictation that did not happen, translating the raised catalogue key. */
  function dictationReason(caught: unknown): string {
    if (caught instanceof DictationRecordingError) {
      return translation(caught.message as Parameters<typeof translation>[0], caught.values);
    }
    return errorMessage(caught);
  }

  async function handleAttachClick() {
    if (readOnly) return;
    if (isTauri()) {
      const paths = await pickDesktopFilePaths();
      await attachByPaths(paths);
      return;
    }
    fileInputRef.current?.click();
  }

  // Native file drops arrive on the webview's own stream with real paths, so the listener registers once and reads a ref.
  const desktopDropRef = useRef<(paths: string[]) => void>(() => {});
  useEffect(() => {
    desktopDropRef.current = (paths: string[]) => {
      void (async () => {
        await attachByPaths(paths);
      })();
    };
  });

  useEffect(() => {
    if (readOnly || !isTauri()) return;
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    // A file dropped anywhere on the window attaches here, matching the desktop convention.
    void watchDesktopFileDrop((event) => {
      if (event.phase === "leave") {
        setDragActive(false);
        return;
      }
      if (event.phase === "drop") {
        setDragActive(false);
        desktopDropRef.current(event.paths);
        return;
      }
      // enter / over: a file is hovering the window — cue the drop affordance.
      setDragActive(true);
    }).then((fn) => {
      if (cancelled) fn();
      else unlisten = fn;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [readOnly]);

  function removeAttachment(uploadId: string) {
    setAttachments((current) => current.filter((attachment) => attachment.upload_id !== uploadId));
  }

  async function handleSubmit() {
    const trimmed = inputValue.trim();
    // A typed message is always required: an attachment is context on top of what you say, never a substitute.
    if (!trimmed) return;
    if (!directoryAvailable) return;
    if (uploadingCount > 0) return;
    setSendPending(true);
    const sendText = trimmed;
    const dataParts = attachments.length > 0 ? [{ kind: "attachments", attachments }] : [];
    // The message is committed the moment Enter is pressed: the optimistic transcript row and the
    // outbox card both show it, so the composer must not hold the same text until the daemon accepts
    // the send, or the message appears in two places at once.
    setHistoryIndex(-1);
    setInputValue("");
    latestInputValueRef.current = "";
    onDraftChange?.("");
    setAttachments([]);
    // Persist to backend and prepend to local list for immediate recall.
    if (trimmed) {
      setMessageHistory((previous) => [trimmed, ...previous]);
      if (workingDirectory) {
        saveMessageHistory(workingDirectory, trimmed).catch((caught) =>
          reportError({ component: "chat-input", operation: "save the message history" }, caught),
        );
      }
    }
    try {
      // While the agent is busy this enqueues for the next turn (handled upstream). Still awaited so
      // a refusal or failure settles through the queue and keeps the send gated.
      await onSend(sendText, dataParts);
    } finally {
      setSendPending(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
      return;
    }
    if (
      event.key === "ArrowUp" &&
      messageHistory.length > 0 &&
      inputRef.current?.selectionStart === 0
    ) {
      event.preventDefault();
      // Save the current draft when first navigating up, so it returns when the user comes back down.
      if (historyIndex === -1) {
        draftInputRef.current = inputValue;
      }
      const nextIndex =
        historyIndex === -1 ? 0 : Math.min(messageHistory.length - 1, historyIndex + 1);
      setHistoryIndex(nextIndex);
      setInputValue(messageHistory[nextIndex]);
      return;
    }
    if (event.key === "ArrowDown" && inputRef.current?.selectionStart === inputValue.length) {
      const nextIndex = historyIndex <= 0 ? -1 : historyIndex - 1;
      setHistoryIndex(nextIndex);
      // Restore the saved draft when navigating back to the "no history" position.
      setInputValue(nextIndex === -1 ? draftInputRef.current : messageHistory[nextIndex]);
      event.preventDefault();
      return;
    }
  }

  return (
    <fieldset
      disabled={composerClosed}
      aria-disabled={composerClosed}
      style={{
        position: "relative",
        padding: 0,
        paddingBottom: "var(--chakra-spacing-2)",
        margin: 0,
        border: 0,
        minWidth: 0,
        opacity: composerClosed ? 0.55 : 1,
        filter: composerClosed ? "grayscale(1)" : undefined,
        transition: "opacity 120ms ease",
      }}
    >
      <ConfirmDialog
        open={compactConfirmOpen}
        onOpenChange={setCompactConfirmOpen}
        title={translation("compactTitle")}
        confirmLabel={translation("compactConfirm")}
        confirmIcon={<LuFoldVertical size={14} />}
        onConfirm={() => onCompact?.()}
      >
        {translation.rich("compactBody", richTags)}
      </ConfirmDialog>

      {/* Message input */}
      <Box px={0} mt={2} pb={1.5}>
        {/* Pending attachments sit above the composer box, so the media cards have room and the input stays clear. */}
        {attachments.length > 0 || uploadingCount > 0 ? (
          <Flex gap={2} pb={2} flexWrap="wrap">
            {attachments.map((attachment) => (
              <AttachmentChip
                key={attachment.upload_id}
                attachment={{
                  filename: attachment.filename,
                  path: attachment.path,
                  mimeType: attachment.mime_type,
                  size: attachment.size,
                }}
                onRemove={() => removeAttachment(attachment.upload_id)}
              />
            ))}
            {uploadingCount > 0 ? (
              <Flex
                align="center"
                gap={1.5}
                px={1.5}
                py={1}
                border="1px solid"
                borderColor="border"
                borderRadius="md"
                bg="bg.subtle"
              >
                <Box color="blue.fg">
                  <LuPaperclip size={14} />
                </Box>
                <Text fontSize="xs" color="fg.subtle">
                  {translation("uploading", { count: uploadingCount })}
                </Text>
              </Flex>
            ) : null}
          </Flex>
        ) : null}
        {/* Aligned to the bottom rather than stretched, so a single line of text is centred by construction. */}
        <Flex data-composer-toolbar="" align="flex-end" gap={2}>
          <Box
            ref={dropZoneRef}
            display="flex"
            flex={1}
            minW={0}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={(event) => {
              event.preventDefault();
              setDragActive(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragActive(false);
              void handleFiles(event.dataTransfer.files);
            }}
          >
            <Textarea
              ref={inputRef}
              // Sized in `globals.css`, where the control height it has to match is defined.
              data-composer-input=""
              size="sm"
              variant="outline"
              placeholder={
                readOnly
                  ? translation("readOnly")
                  : // Ordered by what the person can do about it, so an open decision says what happens to what you type.
                    disabled
                    ? translation("placeholderConnecting")
                    : awaitingDecision
                      ? translation("placeholderAwaitingDecision")
                      : !directoryAvailable
                        ? translation("placeholderInvalidPath")
                        : attachments.length > 0
                          ? translation("placeholderAttachments")
                          : isCompacting
                            ? // Compaction runs as a turn, so this says the message drains when the compaction is done rather than next turn.
                              translation("placeholderCompacting")
                            : isStreaming
                              ? translation("placeholderStreaming")
                              : translation("placeholderDefault")
              }
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={composerClosed || readOnly}
              rows={1}
              fieldSizing="content"
              maxH="44"
              overflowY="auto"
              borderColor={dragActive ? "blue.muted" : "border"}
              bg={composerClosed ? "bg.muted" : "bg.panel"}
              resize="none"
            />
          </Box>
          {/* The same gap as the row of controls below, so the composer has one rhythm rather than two. */}
          <Flex align="flex-end" gap={2} flexShrink={0}>
            <Input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => {
                if (event.target.files) void handleFiles(event.target.files);
                event.target.value = "";
              }}
            />
            {dictationEnabled && (
              <Tooltip
                content={
                  recording
                    ? translation("dictationStop")
                    : transcribing
                      ? translation("dictationTranscribing")
                      : dictationState === "loading"
                        ? translation("dictationLoading")
                        : translation("dictationStart")
                }
                openDelay={200}
                positioning={{ placement: "top" }}
              >
                <IconButton
                  aria-label={
                    recording ? translation("dictationStop") : translation("dictationStart")
                  }
                  onClick={() => void handleDictationClick()}
                  size="sm"
                  // Recording is a state the machine is in, so it is coloured rather than outlined and cannot be left open unnoticed.
                  variant={recording ? "solid" : "outline"}
                  colorPalette={recording ? "red" : undefined}
                  bg={recording ? undefined : "bg"}
                  borderColor={recording ? undefined : "border"}
                  // The spinner covers both waits, and the button is disabled while loading rather than hidden.
                  loading={transcribing || dictationState === "loading"}
                  disabled={
                    composerClosed ||
                    readOnly ||
                    !directoryAvailable ||
                    dictationState === "loading"
                  }
                >
                  {recording ? <LuMicOff /> : <LuMic />}
                </IconButton>
              </Tooltip>
            )}
            <Tooltip
              content={attachmentTooltipContent}
              rich
              openDelay={200}
              closeDelay={60}
              positioning={{ placement: "top" }}
            >
              <IconButton
                aria-label={translation("attachFiles")}
                onClick={() => void handleAttachClick()}
                size="sm"
                variant="outline"
                bg="bg"
                borderColor="border"
                disabled={composerClosed || readOnly || !directoryAvailable}
              >
                <LuPaperclip />
              </IconButton>
            </Tooltip>
            {isStreaming ? (
              <StopTurnButton onAbort={onAbort} isCompacting={isCompacting} />
            ) : (
              <ComposerActionButton
                colorPalette="blue"
                onClick={() => void handleSubmit()}
                busy={sendPending}
                disabled={
                  sendPending ||
                  composerClosed ||
                  readOnly ||
                  !directoryAvailable ||
                  uploadingCount > 0 ||
                  !inputValue.trim()
                }
                label={sendPending ? translation("sending") : translation("send")}
                icon={
                  <ComposerIcon draw={22}>
                    <LuArrowUp />
                  </ComposerIcon>
                }
              />
            )}
          </Flex>
        </Flex>
      </Box>

      {/* One complete line that can be swiped sideways when the viewport is narrow. */}
      <Box position="relative" minW={0}>
        <Flex
          ref={selectorsScrollRef}
          align="center"
          gap={2}
          flexWrap="nowrap"
          px={0}
          pt={1}
          pb={2}
          overflowX="auto"
          overflowY="hidden"
          onScroll={onSelectorsScroll}
          css={{ ...hideHorizontalScrollbar, overflowClipMargin: "3px" }}
        >
          <AgentSelectControl
            agents={agents}
            value={selectedAgent}
            onChange={onAgentChange}
            disabled={!!sessionId || readOnly}
            placeholder={translation("agentPlaceholder")}
          />
          <ModelSelect
            models={models}
            providers={modelProviders}
            recent={recentModels}
            value={agentModel}
            onChange={onAgentModelChange}
            fallbackModelId={agentModel}
            onRetryModels={onRetryModels}
            disabled={readOnly}
            compact
          />
          {/* Adjustable at any point in a session's life, so a conversation need not restart to run under a looser mode. */}
          <PermissionModeControl
            value={permissionMode}
            onChange={(mode) => onPermissionModeChange?.(mode)}
            disabled={readOnly}
          />
          {/* The same control Settings shows, so the two can never disagree about what a mode looks like. */}
          <SandboxToggleControl
            enforce={sandboxEnforce}
            backend={sandboxBackend}
            onChange={onSandboxEnforceChange}
          />
          {/* What the turn has spent, pushed to the far end by a margin rather than by a spacer the fit would have to ignore. */}
          <Flex ms="auto" align="center" gap={2} flexShrink={0}>
            {/* Offered whenever there is a conversation to compaction: when to do it is the reader's judgement, not a threshold's. */}
            {onCompact && !!sessionId && (
              <Button
                variant="outline"
                h="var(--control-height)"
                // Stated rather than inherited, because the button recipe's own gap is not the one this row uses.
                gap={1.5}
                px={2}
                justifyContent="center"
                bg="bg"
                borderColor="border"
                flexShrink={0}
                disabled={isStreaming || isCompacting}
                onClick={() => setCompactConfirmOpen(true)}
                title={
                  isCompacting ? translation("compactingTooltip") : translation("compactTooltip")
                }
              >
                {isCompacting ? (
                  <Spinner boxSize={`${14}px`} borderWidth="1.5px" />
                ) : (
                  <LuFoldVertical size={14} />
                )}
                <Text>{isCompacting ? translation("compacting") : translation("compact")}</Text>
              </Button>
            )}
            <ContextUsageChip tokenUsage={tokenUsage} chatgptUsage={chatgptUsage} />
            <TasksChip tasks={tasks} />
          </Flex>
        </Flex>
        {selectorsHiddenStart ? <Box css={fadeOverlayInline("left", FADE_INLINE)} /> : null}
        {selectorsHiddenEnd ? <Box css={fadeOverlayInline("right", FADE_INLINE)} /> : null}
      </Box>
    </fieldset>
  );
}
