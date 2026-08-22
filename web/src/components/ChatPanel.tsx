"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  Heading,
  IconButton,
  Menu,
  Separator,
  Spinner,
  Text,
  VStack,
} from "@chakra-ui/react";
import {
  LuArrowDown,
  LuBookMarked,
  LuClipboardCheck,
  LuDot,
  LuEllipsis,
  LuFolderOpen,
  LuGitBranch,
  LuMessageSquare,
  LuMoon,
  LuPanelLeftClose,
  LuPanelLeftOpen,
  LuPlugZap,
  LuSettings,
  LuSun,
  LuTerminal,
  LuTrash2,
  LuTriangleAlert,
} from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { FadeIn, fadeSurfaceTransition } from "@/components/ui/FadeIn";
import { useTranslations } from "next-intl";
import { toaster } from "@/components/ui/Toaster";
import { PanelTiles, type TilePanel } from "./PanelTiles";
import { useColorMode } from "./ui/ColorMode";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import { useChat } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup, UserMessageCard } from "./ChatMessage";
import { TranscriptWaitRow } from "./ToolGroup";
import { TOP_BAR_HEIGHT } from "@/components/ui/Panel";
import { ChatInput } from "./ChatInput";
import { QuestionOverlay } from "./QuestionOverlay";
import { SettingsDialog, type SettingsSection } from "./SettingsDialog";
import { BackgroundJobsPanel } from "./BackgroundJobsPanel";
import { MemoryPanel } from "./MemoryPanel";
import { DelegatedWorkPanel } from "./DelegatedWorkPanel";
import { GoalReviewPanel } from "./GoalReviewPanel";
import type { SessionEntry } from "./SessionRow";
import { GitStatusBar } from "./GitStatusBar";
import { GoalBar } from "./GoalBar";
import { LocationChip } from "./LocationStatus";
import { SectionHeader } from "./ui/SectionHeader";
import { CONCEPT_ICONS } from "@/lib/glyphs";
import { useDirectoryStatus } from "@/lib/use-directory-status";
import { Tooltip } from "./ui/Tooltip";
import { ToolbarAction } from "@/components/ui/Toolbar";
import { AgentNamesProvider } from "@/lib/agent-names";
import { DropdownMenu } from "@/components/ui/Menu";
import { PermissionOverlay } from "./PermissionOverlay";
import { AgentSkills } from "./AgentSkills";
import { getToolCallDisplay } from "@/lib/glyphs";
import {
  permissionReasonPaths,
  permissionReasonText,
  type ToolPermission,
  type ToolQuestion,
} from "@/lib/tool-event";

import {
  clearSessionGoal,
  resumeSessionGoal,
  fetchAgentConfiguration,
  getWorkspace,
  revealInFinder,
  saveSessionDraft,
  saveAgentConfiguration,
  saveSettings,
  setSessionPermissionMode,
  subscribeEvents,
  type AgentCard,
  type AgentSummary,
  type Location,
  type PermissionMode,
  type SandboxEnforce,
  type WorktreeStrategy,
} from "@/lib/api";
import {
  hideHorizontalScrollbar,
  FADE_INLINE,
  fadeOverlayInline,
  scrollFade,
  scrollFadeTopBottom,
  useScrollInlineFade,
} from "@/lib/scroll-fade";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { playAttentionSound, playTurnEndSound } from "@/lib/sounds";
import {
  closePermissionNotification,
  notifyPermissionRequest,
  setPermissionNotificationHandler,
} from "@/lib/notify";
import { reportError } from "@/lib/faults";
import { errorMessage } from "@/lib/errors";
import { isCompactViewport } from "@/lib/viewport";
import { timelineItems, turnHasVisibleOutput } from "@/lib/chat-timeline";

// A Chakra Box that is also a motion component, so the right region can animate open and closed without losing its flex props.
const MotionBox = motion.create(Box);

// The panels that can share the right-hand region: the terminal and background pair, and delegated work.
export type SidePanelKey = "background" | "delegated" | "memory" | "reviews";

const MAXIMUM_OPEN_SIDE_PANELS = 2;

// One shared empty set for a caller tracking no unread completions, so the tree panel is not re-rendered for a fresh Set.
const EMPTY_UNSEEN_COMPLETIONS: Set<string> = new Set();

interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  // When set, the workspace opens with the Settings dialog already showing this section.
  initialSettingsSection?: string;
  initialSessionId: string | null;
  // The session's display title, shown in the top bar and absent until the session names itself.
  sessionTitle?: string;
  initialInputDraft?: string;
  // Deletes the session by id and routes back to a blank chat; absent when there is no active session.
  onDeleteSession?: (sessionId: string) => void;
  // Every session the client knows, since the delegated-work panel draws the relationships between them.
  sessions?: SessionEntry[];
  // Sessions that finished while you were looking elsewhere, so a row carries the same unread mark as the sidebar.
  unseenCompletions?: Set<string>;
  // The conversation the sidebar has selected, so reading a delegated session does not change what the panel shows.
  rootSessionId?: string | null;
  // Opens a session from the panel; the page owns which conversation is open, so the panel asks rather than switches.
  onResumeSession?: (entry: SessionEntry) => void;
  // Which side panels are open and how wide the region is, held by the page because this component remounts per conversation.
  openSidePanels: SidePanelKey[];
  onOpenSidePanelsChange: (panels: SidePanelKey[]) => void;
  sidePanelWidth: number;
  onSidePanelWidthChange: (width: number) => void;
  initialPermissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  sessionRunning?: boolean;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  workspaceId?: string;
  homeDirectory?: string;
  sandboxEnforce?: SandboxEnforce;
  sandboxBackend?: { backend: string; detail: string };
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void;
  worktreeStrategy?: WorktreeStrategy;
  onWorktreeStrategyChange?: (strategy: WorktreeStrategy) => void | Promise<void>;
  // Whether this panel is ready to hold a conversation, which at startup is a wait rather than a failure.
  isConnected?: boolean;
  // Whether the daemon itself is unreachable, the one state with a remedy and so the only one that earns the error screen.
  connectionLost?: boolean;
  // Asks the page to fetch everything a lost daemon took away.
  onReconnect?: () => void | Promise<void>;
  reconnecting?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean }[];
  modelProviders?: {
    id: string;
    name: string;
    openai_compatible: boolean;
    credential_id: string;
  }[];
  recentModels?: { id: string; name: string; provider: string }[];
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  // Re-fetches the model catalog through the daemon, the retry path for a failed initial load.
  onRetryModels?: () => void | Promise<void>;
}

function folderDisplayName(workingDirectory?: string): string {
  const directory = (workingDirectory ?? "").trim();
  if (!directory) return "";
  return directory.split(/[\\/]/).filter(Boolean).at(-1) ?? directory;
}

export function ChatPanel({
  agent,
  agents,
  agentCard,
  onAgentChange,
  initialSettingsSection,
  initialSessionId,
  sessionTitle,
  initialInputDraft = "",
  onDeleteSession,
  sessions = [],
  unseenCompletions,
  rootSessionId = null,
  onResumeSession,
  openSidePanels,
  onOpenSidePanelsChange,
  sidePanelWidth,
  onSidePanelWidthChange,
  initialPermissionMode = "ask",
  onPermissionModeChange,
  sessionRunning = false,
  onSessionCreated,
  workingDirectory,
  workspaceId = "",
  homeDirectory,
  sandboxEnforce = "required" as SandboxEnforce,
  sandboxBackend = { backend: "", detail: "" },
  onSandboxEnforceChange,
  worktreeStrategy = "none",
  onWorktreeStrategyChange,
  isConnected = true,
  connectionLost = false,
  onReconnect,
  reconnecting = false,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  onRetryModels,
}: ChatPanelProps) {
  const translation = useTranslations("ChatPanel");
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>(initialPermissionMode);
  const persistedPermissionModeRef = useRef(initialPermissionMode);
  const permissionRequestRef = useRef(0);
  const permissionSyncRef = useRef<Promise<void>>(Promise.resolve());
  const {
    messages,
    tasks,
    tokenUsage,
    queuedMessages,
    handedMessages,
    awaitingModel,
    sessionId,
    isStreaming,
    isHistoryLoading,
    isHistoryStreaming,
    historyError,
    reloadHistory,
    send,
    abort,
    dequeueMessage,
    outboxHold,
    grantedPermissionMode,
    retryOutbox,
    retryTurn,
    handlePermission,
    handleQuestion,
    declineQuestion,
    compact,
  } = useChat(
    agent,
    initialSessionId,
    workingDirectory,
    worktreeStrategy,
    permissionMode,
    sessionRunning,
    workspaceId,
  );

  // One source of truth for the working directory, optimistic until validation explicitly rejects it.
  const { status: directoryStatus, directoryAvailable } = useDirectoryStatus(workingDirectory);
  // Mounted whenever a daemon is there: a directory check runs on every switch, and unmounting on it jumped.
  const chatReady = isConnected;
  // The one condition under which the transcript is in the DOM, read by the render and by everything touching scroll.
  const transcriptVisible = chatReady && !isHistoryLoading;
  // The workspace's locations for the terminal picker, refreshed when the workspace configuration changes.
  const [workspaceLocations, setWorkspaceLocations] = useState<Location[]>([]);
  const [locationsReady, setLocationsReady] = useState(false);
  const [skillsReady, setSkillsReady] = useState(false);
  const welcomeReady = locationsReady && skillsReady;
  useEffect(() => {
    let cancelled = false;
    setLocationsReady(false);
    // Resolving through a promise keeps the state update off the synchronous effect path.
    const load = () => {
      const request = workspaceId ? getWorkspace(workspaceId) : Promise.resolve(null);
      request
        .then((workspace) => {
          if (!cancelled) setWorkspaceLocations(workspace?.locations ?? []);
        })
        .catch((caught) => {
          reportError({ component: "chat-panel", operation: "read a workspace" }, caught);
          if (!cancelled) setWorkspaceLocations([]);
        })
        .finally(() => {
          if (!cancelled) setLocationsReady(true);
        });
    };
    load();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "workspaces_changed") load();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [workspaceId]);

  // A new conversation starts with the selected agent's mode and mirrors it to the machine setting.
  useEffect(() => {
    if (initialSessionId || !agent) return;
    let cancelled = false;
    const requestVersion = ++permissionRequestRef.current;
    fetchAgentConfiguration(agent, workingDirectory)
      .then((configuration) => {
        if (cancelled || requestVersion !== permissionRequestRef.current) return;
        const nextMode = configuration.permission_mode;
        persistedPermissionModeRef.current = nextMode;
        setPermissionModeState(nextMode);
        onPermissionModeChange?.(nextMode);
        const mirrorAgentMode = async () => {
          if (cancelled || requestVersion !== permissionRequestRef.current) return;
          await saveSettings({ permission_mode: nextMode });
          persistedPermissionModeRef.current = nextMode;
        };
        permissionSyncRef.current = permissionSyncRef.current.then(
          mirrorAgentMode,
          mirrorAgentMode,
        );
        return permissionSyncRef.current;
      })
      .catch((caught) =>
        reportError(
          { component: "chat-panel", operation: "load the agent permission mode" },
          caught,
        ),
      );
    return () => {
      cancelled = true;
    };
  }, [agent, initialSessionId, onPermissionModeChange, workingDirectory]);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  // Following the bottom, released the moment the user scrolls up and resumed when they return.
  const isPinnedRef = useRef(true);
  const onStreamingChangeRef = useRef(onStreamingChange);
  const notifiedSessionIdRef = useRef<string | null>(null);
  const backgroundPanelOpen = openSidePanels.includes("background");
  const memoryPanelOpen = openSidePanels.includes("memory");
  const delegatedPanelOpen = openSidePanels.includes("delegated");
  const reviewPanelOpen = openSidePanels.includes("reviews");
  const [selectedGoalReviewId, setSelectedGoalReviewId] = useState<string | null>(null);
  const { colorMode, toggleColorMode } = useColorMode();
  // Whether the transcript is near the bottom, driving the jump-to-latest affordance.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // A strict at-bottom flag driving the transcript's bottom fade, so the last line is never dimmed.
  const [transcriptPinned, setTranscriptPinned] = useState(true);
  // Top-bar surfaces, opened on mount when the workspace was entered with `?settings=<section>`.
  const validInitialSection: SettingsSection | null =
    initialSettingsSection === "general" ||
    initialSettingsSection === "locations" ||
    initialSettingsSection === "agents" ||
    initialSettingsSection === "connection"
      ? initialSettingsSection
      : null;
  const [settingsOpen, setSettingsOpen] = useState(!!validInitialSection);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(
    validInitialSection ?? "general",
  );
  const [appliedInitialSettingsSection, setAppliedInitialSettingsSection] =
    useState(validInitialSection);
  if (appliedInitialSettingsSection !== validInitialSection) {
    setAppliedInitialSettingsSection(validInitialSection);
    if (validInitialSection) {
      setSettingsSection(validInitialSection);
      setSettingsOpen(true);
    }
  }
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const setSidePanelOpen = useCallback(
    (panel: SidePanelKey, open: boolean) => {
      const remainingPanels = openSidePanels.filter((openPanel) => openPanel !== panel);
      if (isCompactViewport()) {
        const activePanel = openSidePanels[openSidePanels.length - 1];
        onOpenSidePanelsChange(!open && activePanel === panel ? [] : [panel]);
        return;
      }
      onOpenSidePanelsChange(
        open ? [...remainingPanels, panel].slice(-MAXIMUM_OPEN_SIDE_PANELS) : remainingPanels,
      );
    },
    [openSidePanels, onOpenSidePanelsChange],
  );

  useEffect(() => {
    if (
      openSidePanels.length === 0 ||
      !historyOpen ||
      !window.matchMedia("(max-width: 1199px)").matches
    )
      return;
    onToggleHistory?.();
  }, [openSidePanels.length, historyOpen, onToggleHistory]);

  const markSidePanelActive = useCallback(
    (panel: SidePanelKey) => {
      if (!openSidePanels.includes(panel) || openSidePanels[openSidePanels.length - 1] === panel)
        return;
      onOpenSidePanelsChange([...openSidePanels.filter((openPanel) => openPanel !== panel), panel]);
    },
    [openSidePanels, onOpenSidePanelsChange],
  );

  const openGoalReview = useCallback(
    (reviewId: string) => {
      setSelectedGoalReviewId(reviewId);
      setSidePanelOpen("reviews", true);
    },
    [setSidePanelOpen],
  );

  // The goal bar means the review taking place now: reset the selection so the panel follows the
  // newest review, since a stale selection would otherwise keep it on an older transcript.
  const openCurrentReview = useCallback(() => {
    setSelectedGoalReviewId(null);
    setSidePanelOpen("reviews", true);
  }, [setSidePanelOpen]);

  // A review opening is the cue to surface its transcript: the panel follows the newest review automatically.
  const seenReviewIds = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!sessionId) return;
    return subscribeEvents((event) => {
      const changed = event as { type: string; session?: string; review?: string };
      if (changed.type !== "goal_reviews_changed" || changed.session !== sessionId) return;
      const reviewId = changed.review;
      if (!reviewId || seenReviewIds.current.has(reviewId)) return;
      seenReviewIds.current.add(reviewId);
      openGoalReview(reviewId);
    });
  }, [openGoalReview, sessionId]);

  // Pinned means the viewport is at the bottom: pinned follows new content, unpinned never pulls the reader back down.
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    // A column-reverse scroller's latest edge is always the zero origin. Engines differ
    // only on whether travel toward history is reported positive or negative.
    const distanceFromBottom = Math.abs(container.scrollTop);
    const atBottom = distanceFromBottom <= 8;
    isPinnedRef.current = atBottom;
    setTranscriptPinned(atBottom);
    // A larger threshold for the jump button than for pinning, so the button does not flash while pinning stays strict.
    setIsAtBottom(distanceFromBottom <= 120);
  }, []);

  useEffect(() => {
    onStreamingChangeRef.current = onStreamingChange;
  }, [onStreamingChange]);

  const scrollToBottom = useCallback(() => {
    isPinnedRef.current = true;
    setIsAtBottom(true);
    setTranscriptPinned(true);
    const container = scrollContainerRef.current;
    if (!container) return;
    container.scrollTop = 0;
  }, []);

  const handleSend = useCallback(
    (text: string, dataParts?: Record<string, unknown>[]) => {
      // Which agent runs is never assumed, so an unchosen agent is asked for rather than guessed at.
      if (!agent && !initialSessionId) {
        toaster.create({
          type: "error",
          title: translation("chooseAnAgent"),
          description: translation("chooseAnAgentDescription"),
          closable: true,
        });
        return undefined;
      }
      scrollToBottom();
      // Whether this can be sent at all is `useChat`'s to answer rather than the composer's.
      const result = send(text, dataParts);
      scrollToBottom();
      return result;
    },
    [agent, initialSessionId, scrollToBottom, send, translation],
  );

  // The chip follows the mode the session actually runs under, which is the whole of what a person needs to know.
  const effectivePermissionMode = grantedPermissionMode ?? permissionMode;
  const announcedClampRef = useRef<string>("");
  useEffect(() => {
    if (!grantedPermissionMode || announcedClampRef.current === grantedPermissionMode) return;
    announcedClampRef.current = grantedPermissionMode;
    onPermissionModeChange?.(grantedPermissionMode);
  }, [grantedPermissionMode, onPermissionModeChange]);

  // The session's goal, read from the session list the daemon already pushes rather than kept as a second copy.
  const activeGoal = useMemo(
    () => sessions.find((entry) => entry.sessionId === sessionId)?.goal ?? null,
    [sessions, sessionId],
  );
  const handleClearGoal = useCallback(() => {
    if (!sessionId) return;
    // The bar clears when the daemon says it cleared, since whether the goal was still there is the session's answer.
    clearSessionGoal(sessionId).catch((caught) =>
      reportError({ component: "chat-panel", operation: "call off the goal" }, caught),
    );
  }, [sessionId]);

  const handleResumeGoal = useCallback(() => {
    if (!sessionId) return;
    resumeSessionGoal(sessionId).catch((caught) =>
      reportError({ component: "chat-panel", operation: "restart the parked goal" }, caught),
    );
  }, [sessionId]);

  const openSettings = useCallback(
    (section: SettingsSection) => {
      setSettingsSection(section);
      setSettingsOpen(true);
    },
    [setSettingsOpen, setSettingsSection],
  );

  useEffect(() => {
    if (!sessionId || notifiedSessionIdRef.current === sessionId) return;
    notifiedSessionIdRef.current = sessionId;
    onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  const timelineMounted = transcriptVisible && !historyError && messages.length > 0;
  const compactionFailed = messages.some(
    (message) => message.role === "compaction" && message.meta?.status === "failed",
  );

  const [permissionSource, setPermissionSource] = useState({
    sessionId: initialSessionId,
    mode: initialPermissionMode,
  });
  if (
    permissionSource.sessionId !== initialSessionId ||
    permissionSource.mode !== initialPermissionMode
  ) {
    setPermissionSource({ sessionId: initialSessionId, mode: initialPermissionMode });
    setPermissionModeState(initialPermissionMode);
  }

  useLayoutEffect(() => {
    persistedPermissionModeRef.current = initialPermissionMode;
  }, [initialPermissionMode, initialSessionId]);

  useLayoutEffect(() => {
    isPinnedRef.current = true;
    const container = scrollContainerRef.current;
    if (container) container.scrollTop = 0;
  }, [initialSessionId]);

  // Surface the streaming flag to the parent; new content is followed by the layout effect below.
  useEffect(() => {
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming]);

  // Keep the machine, selected agent, and active session on the same mode in request order.
  function handlePermissionModeChange(nextMode: PermissionMode) {
    const requestVersion = ++permissionRequestRef.current;
    const targetAgent = agent;
    const targetSession = sessionId;
    setPermissionModeState(nextMode);
    onPermissionModeChange?.(nextMode);
    const synchronize = async () => {
      const previousMode = persistedPermissionModeRef.current;
      try {
        await saveSettings({ permission_mode: nextMode });
        await saveAgentConfiguration(targetAgent, { permission_mode: nextMode }, workingDirectory);
        const appliedMode = targetSession
          ? await setSessionPermissionMode(targetSession, nextMode)
          : nextMode;
        persistedPermissionModeRef.current = appliedMode;
        if (requestVersion === permissionRequestRef.current) {
          setPermissionModeState(appliedMode);
          onPermissionModeChange?.(appliedMode);
        }
      } catch (caught) {
        await Promise.allSettled([
          saveSettings({ permission_mode: previousMode }),
          saveAgentConfiguration(targetAgent, { permission_mode: previousMode }, workingDirectory),
          targetSession
            ? setSessionPermissionMode(targetSession, previousMode)
            : Promise.resolve(previousMode),
        ]);
        persistedPermissionModeRef.current = previousMode;
        if (requestVersion !== permissionRequestRef.current) return;
        setPermissionModeState(previousMode);
        onPermissionModeChange?.(previousMode);
        toaster.create({
          type: "error",
          title: translation("permissionModeFailedTitle"),
          description: errorMessage(caught),
          closable: true,
        });
      }
    };
    permissionSyncRef.current = permissionSyncRef.current.then(synchronize, synchronize);
    return permissionSyncRef.current;
  }

  async function handleSettingsPermissionModeSaved(nextMode: PermissionMode) {
    const appliedMode = sessionId ? await setSessionPermissionMode(sessionId, nextMode) : nextMode;
    persistedPermissionModeRef.current = appliedMode;
    setPermissionModeState(appliedMode);
    onPermissionModeChange?.(appliedMode);
  }

  const handleInputDraftChange = useCallback(
    (nextDraft: string) => {
      if (!sessionId) return;
      saveSessionDraft(sessionId, nextDraft).catch((caught) =>
        reportError({ component: "chat-panel", operation: "save the session draft" }, caught),
      );
    },
    [sessionId],
  );

  const currentCompactionerName =
    folderDisplayName(workingDirectory) || translation("thisCompactioner");
  const renderedTimeline = useMemo(() => timelineItems(messages), [messages]);
  // The daemon's awaiting_model status, only before this turn has a visible row: repeating it
  // between tool calls flashes the layout.
  const awaitingProvider = isStreaming && awaitingModel && !turnHasVisibleOutput(messages);
  const visibleHandedMessages = useMemo(() => {
    if (handedMessages.length === 0) return handedMessages;
    const presentIds = new Set(
      messages.filter((message) => message.role === "user").map((message) => message.id),
    );
    return handedMessages.filter((message) => !presentIds.has(`user-${message.id}`));
  }, [messages, handedMessages]);
  // The review a user message's turn started, so its transcript is reachable from that row.
  const reviewIdByUserMessage = useMemo(() => {
    const byUser = new Map<string, string>();
    let lastUserMessageId = "";
    for (const item of renderedTimeline) {
      if (item.kind !== "message") continue;
      const message = item.message;
      if (message.role === "user") {
        lastUserMessageId = message.id;
        continue;
      }
      if (message.role === "goal" && message.meta?.goalReviewId) {
        if (lastUserMessageId) byUser.set(lastUserMessageId, String(message.meta.goalReviewId));
      }
    }
    return byUser;
  }, [renderedTimeline]);
  const hasInheritedContext = Boolean(
    initialSessionId &&
    sessions.some((entry) => entry.sessionId === initialSessionId && entry.parentSessionId),
  );
  // Entrance animation is reserved for rows a live turn just appended at the bottom, by a purely positional rule.
  const timelineKeys = renderedTimeline.map((item) =>
    item.kind === "tool_group" ? item.id : item.message.id,
  );
  const timelineSessionKey = initialSessionId ?? "__new__";
  const [timelineAnimationState, setTimelineAnimationState] = useState<{
    sessionKey: string;
    keys: string[];
    seen: string[];
    animated: string[];
  }>(() => ({ sessionKey: timelineSessionKey, keys: [], seen: [], animated: [] }));
  let animatedKeys = new Set(timelineAnimationState.animated);
  const timelineKeysChanged =
    timelineAnimationState.sessionKey !== timelineSessionKey ||
    timelineAnimationState.keys.length !== timelineKeys.length ||
    timelineAnimationState.keys.some((key, index) => key !== timelineKeys[index]);
  if (timelineKeysChanged) {
    const previousSeen =
      timelineAnimationState.sessionKey === timelineSessionKey
        ? new Set(timelineAnimationState.seen)
        : new Set<string>();
    const nextAnimated = new Set<string>();
    // Only when this is the same transcript with something added, since a replay rebuilds every row under a different id.
    const survived = timelineKeys.some((key) => previousSeen.has(key));
    if (previousSeen.size > 0 && survived) {
      for (let index = timelineKeys.length - 1; index >= 0; index -= 1) {
        if (previousSeen.has(timelineKeys[index])) break;
        nextAnimated.add(timelineKeys[index]);
      }
    }
    const nextSeen = Array.from(new Set([...previousSeen, ...timelineKeys]));
    setTimelineAnimationState({
      sessionKey: timelineSessionKey,
      keys: timelineKeys,
      seen: nextSeen,
      animated: Array.from(nextAnimated),
    });
    animatedKeys = nextAnimated;
  }
  // Running shell commands drive the badge on the background-processes button.
  const runningShellCount = messages.filter(
    (message) =>
      message.role === "tool_call" &&
      message.content === "bash" &&
      (message.meta?.status === "running" || message.meta?.status === "input_required"),
  ).length;
  // How many sessions the open conversation has delegated, at any depth, counted against that conversation.
  const delegatedSessionCount = useMemo(() => {
    if (!rootSessionId) return 0;
    const childrenOf = new Map<string, string[]>();
    for (const session of sessions) {
      if (!session.parentSessionId) continue;
      childrenOf.set(session.parentSessionId, [
        ...(childrenOf.get(session.parentSessionId) ?? []),
        session.sessionId,
      ]);
    }
    let total = 0;
    const pending = [rootSessionId];
    while (pending.length > 0) {
      const next = childrenOf.get(pending.pop() as string) ?? [];
      total += next.length;
      pending.push(...next);
    }
    return total;
  }, [sessions, rootSessionId]);
  // A compaction pass is live while its marker is still running, driving the Compact control's in-progress state.
  const isCompacting = messages.some(
    (message) => message.role === "compaction" && message.meta?.status === "running",
  );
  // "Reveal" opens the session's working directory in the OS file manager.
  const revealPath = (workingDirectory || "").trim();

  // Retry continues the existing durable turn. It never creates another user message.
  const handleRetry = useCallback(() => {
    void retryTurn();
  }, [retryTurn]);

  // The first pending prompt, surfaced as an overlay above the input so a decision always grabs attention.
  let pendingPrompt:
    | { kind: "question"; question: ToolQuestion }
    | {
        kind: "permission";
        permission: ToolPermission;
        title: string;
        detail?: string;
        detailPaths?: string[];
        command?: string;
        arguments?: Record<string, unknown>;
      }
    | null = null;
  {
    for (const message of messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const question = message.meta?.question as ToolQuestion | undefined;
      if (question) {
        pendingPrompt = { kind: "question", question };
        break;
      }
      const permission = message.meta?.permission as ToolPermission | undefined;
      // Under `automatic` the reviewer answers the gate, so there is no person to ask: the card
      // itself shows the call is being weighed and the overlay stays away.
      if (permission && effectivePermissionMode !== "automatic") {
        const name = message.content;
        const args = message.meta?.arguments as Record<string, unknown> | undefined;
        const command = name === "bash" && args?.command ? String(args.command) : "";
        pendingPrompt = {
          kind: "permission",
          permission,
          // The title says what the agent is trying to do, and the detail says what made this stop for approval.
          // A call held for approval has finished being written, or there would be nothing to approve.
          title: getToolCallDisplay(name, args, true).label,
          // The structured reason wins, because it is the only one this interface can say in the reader's language.
          detail:
            permissionReasonText(permission.reason, translation) ||
            permission.explanation ||
            undefined,
          // The paths travel as data so the overlay lists them, rather than being joined into a sentence.
          detailPaths: permissionReasonPaths(permission.reason),
          command: command || undefined,
          arguments: args,
        };
        break;
      }
    }
  }
  // The attention cue plays for the first prompt in a turn, and a permission prompt also raises a system notification.
  const permissionTranslation = useTranslations("PermissionOverlay");
  const pendingPermissionId =
    pendingPrompt?.kind === "permission" ? pendingPrompt.permission.requestId : "";
  const pendingQuestionId =
    pendingPrompt?.kind === "question" ? pendingPrompt.question.requestId : "";
  const pendingPermissionBody =
    pendingPrompt?.kind === "permission" ? pendingPrompt.command || pendingPrompt.title : "";
  const notifiedPermissionRef = useRef("");
  const attentionSoundPlayedRef = useRef(false);
  const pendingPromptId = pendingPermissionId || pendingQuestionId;
  useEffect(() => {
    if (!isStreaming && !pendingPromptId) {
      attentionSoundPlayedRef.current = false;
      return;
    }
    if (!pendingPromptId || attentionSoundPlayedRef.current) return;
    attentionSoundPlayedRef.current = true;
    playAttentionSound();
  }, [isStreaming, pendingPromptId]);

  // The turn-end chime, on the transition to actually finished rather than to a pause for a decision.
  const wasRunningRef = useRef(false);
  useEffect(() => {
    const running = isStreaming || !!pendingPromptId;
    if (wasRunningRef.current && !running) playTurnEndSound();
    wasRunningRef.current = running;
  }, [isStreaming, pendingPromptId]);
  useEffect(() => {
    const previous = notifiedPermissionRef.current;
    if (previous && previous !== pendingPermissionId) void closePermissionNotification(previous);
    notifiedPermissionRef.current = pendingPermissionId;
    if (!pendingPermissionId) return;
    void notifyPermissionRequest({
      requestId: pendingPermissionId,
      title: permissionTranslation("approvalNeeded"),
      body: pendingPermissionBody,
      actionLabel: permissionTranslation("allowOnce"),
    });
  }, [pendingPermissionId, pendingPermissionBody, permissionTranslation]);
  // The notification's action button resolves the request exactly like the overlay's primary button.
  useEffect(() => {
    setPermissionNotificationHandler((requestId) => handlePermission(requestId, "allow_once"));
    return () => setPermissionNotificationHandler(null);
  }, [handlePermission]);

  const sessionTranscriptReady =
    connectionLost ||
    historyError ||
    (transcriptVisible && (messages.length > 0 || hasInheritedContext || welcomeReady));

  const startSidePanelResize = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = sidePanelWidth;

      function handlePointerMove(moveEvent: globalThis.PointerEvent) {
        // Clamp to the same bounds the region's CSS enforces, so the drag never fights the styled limits.
        const nextWidth = Math.min(
          Math.min(900, Math.round(window.innerWidth * 0.8)),
          Math.max(360, startWidth + startX - moveEvent.clientX),
        );
        onSidePanelWidthChange(nextWidth);
      }

      function handlePointerUp() {
        window.removeEventListener("pointermove", handlePointerMove);
        window.removeEventListener("pointerup", handlePointerUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }

      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp, { once: true });
    },
    [sidePanelWidth, onSidePanelWidthChange],
  );

  // Built before the return so the region is gated on the tiles themselves, since an open panel with nothing to draw would reserve an empty column.
  const sidePanels = (
    [
      backgroundPanelOpen && {
        key: "background",
        onActivate: () => markSidePanelActive("background"),
        content: (
          <BackgroundJobsPanel
            open={backgroundPanelOpen}
            onClose={() => setSidePanelOpen("background", false)}
            messages={messages}
            sessionId={sessionId}
            workingDirectory={workingDirectory || homeDirectory || ""}
            locations={workspaceLocations}
          />
        ),
      },
      memoryPanelOpen && {
        key: "memory",
        onActivate: () => markSidePanelActive("memory"),
        content: (
          <MemoryPanel
            sessionId={sessionId}
            workingDirectory={workingDirectory || homeDirectory || ""}
            onClose={() => setSidePanelOpen("memory", false)}
          />
        ),
      },
      delegatedPanelOpen && {
        key: "delegated",
        onActivate: () => markSidePanelActive("delegated"),
        content: (
          <DelegatedWorkPanel
            sessions={sessions}
            rootSessionId={rootSessionId}
            activeSessionId={sessionId}
            unseenCompletions={unseenCompletions ?? EMPTY_UNSEEN_COMPLETIONS}
            agents={agents}
            onResume={(entry) => onResumeSession?.(entry)}
            onDeleteSession={(entry) => onDeleteSession?.(entry.sessionId)}
            onClose={() => setSidePanelOpen("delegated", false)}
          />
        ),
      },
      reviewPanelOpen && {
        key: "reviews",
        onActivate: () => markSidePanelActive("reviews"),
        content: (
          <GoalReviewPanel
            sessionId={sessionId}
            selectedReviewId={selectedGoalReviewId}
            onSelectedReviewChange={setSelectedGoalReviewId}
            onClose={() => setSidePanelOpen("reviews", false)}
          />
        ),
      },
    ].filter(Boolean) as TilePanel[]
  ).sort(
    (first, second) =>
      openSidePanels.indexOf(first.key as SidePanelKey) -
      openSidePanels.indexOf(second.key as SidePanelKey),
  );
  const {
    containerRef: topBarScrollRef,
    onScroll: onTopBarScroll,
    hiddenStart: topBarHiddenStart,
    hiddenEnd: topBarHiddenEnd,
  } = useScrollInlineFade();

  return (
    // Every profile name is resolved from this one catalogue, so the transcript and the sidebar cannot disagree.
    <AgentNamesProvider agents={agents}>
      <Flex h="100%" minW={0} position="relative">
        <Flex direction="column" flex={1} minW={0} h="100%">
          {/* Persistent top bar: session identity on the left, session tools on the right. */}
          <Flex align="center" gap={2} px={2} h={TOP_BAR_HEIGHT} flexShrink={0} minW={0}>
            {onToggleHistory ? (
              <Tooltip
                content={
                  historyOpen ? translation("hideConversations") : translation("showConversations")
                }
                openDelay={300}
              >
                <IconButton
                  aria-label={
                    historyOpen
                      ? translation("hideConversationsSidebar")
                      : translation("showConversationsSidebar")
                  }
                  variant="ghost"
                  colorPalette="gray"
                  flexShrink={0}
                  onClick={onToggleHistory}
                >
                  {historyOpen ? <LuPanelLeftClose size={14} /> : <LuPanelLeftOpen size={14} />}
                </IconButton>
              </Tooltip>
            ) : (
              <Box color="fg.muted" flexShrink={0}>
                <LuMessageSquare size={14} />
              </Box>
            )}
            <Box position="relative" flex={1} minW={0} h="full">
            <Flex
              ref={topBarScrollRef}
              align="center"
              gap={2}
              h="full"
              overflowX="auto"
              onScroll={onTopBarScroll}
              css={hideHorizontalScrollbar}
            >
            <Text textStyle="panelTitle" fontWeight="medium" whiteSpace="nowrap" flexShrink={0}>
              {sessionId
                ? sessionTitle || translation("untitledConversation")
                : translation("newConversation")}
            </Text>
            <Box flexShrink={0}>
              <AnimatePresence>
                {directoryStatus.valid && directoryStatus.isGitRepository ? (
                  <motion.div
                    key="git-status"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={fadeSurfaceTransition}
                  >
                    <GitStatusBar status={directoryStatus} />
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </Box>
            <Flex align="center" gap={1} flexShrink={0}>
              {/* What this workspace's conversations have handed to other sessions, with a dot only when something has. */}
              <ToolbarAction
                label={translation("delegatedWork")}
                icon={<LuGitBranch size={14} />}
                active={delegatedPanelOpen}
                colorPalette="purple"
                indicator={delegatedSessionCount > 0}
                onClick={() => setSidePanelOpen("delegated", !delegatedPanelOpen)}
              />
              <ToolbarAction
                label={translation("goalReviews")}
                icon={<LuClipboardCheck size={14} />}
                active={reviewPanelOpen}
                colorPalette="purple"
                onClick={() => setSidePanelOpen("reviews", !reviewPanelOpen)}
              />
              {/* What this conversation remembers of the turns that have left its window. */}
              <ToolbarAction
                label={translation("memory")}
                icon={<LuBookMarked size={14} />}
                active={memoryPanelOpen}
                colorPalette="orange"
                onClick={() => setSidePanelOpen("memory", !memoryPanelOpen)}
              />
              <ToolbarAction
                label={translation("terminalAndBackground")}
                icon={<LuTerminal size={14} />}
                active={backgroundPanelOpen}
                colorPalette="green"
                indicator={runningShellCount > 0}
                onClick={() => setSidePanelOpen("background", !backgroundPanelOpen)}
              />
              {/* Light and dark, switched here because it is the one setting people change on a whim. */}
              <ToolbarAction
                label={
                  colorMode === "dark" ? translation("switchToLight") : translation("switchToDark")
                }
                icon={colorMode === "dark" ? <LuSun size={14} /> : <LuMoon size={14} />}
                onClick={toggleColorMode}
              />
              <ToolbarAction
                label={translation("settings")}
                icon={<LuSettings size={14} />}
                onClick={() => openSettings("general")}
              />
              <DropdownMenu
                trigger={
                  <IconButton aria-label={translation("sessionOptions")} variant="ghost">
                    <LuEllipsis size={14} />
                  </IconButton>
                }
                minW="200px"
              >
                <Menu.Item
                  value="reveal"
                  fontSize="xs"
                  disabled={!revealPath}
                  onClick={() => {
                    if (revealPath) void revealInFinder(revealPath);
                  }}
                >
                  <LuFolderOpen size={13} />
                  <Box flex={1}>{translation("openThisCompactioner")}</Box>
                </Menu.Item>
                <Menu.Item
                  value="delete"
                  fontSize="xs"
                  color="red.fg"
                  _hover={{ bg: "red.subtle" }}
                  disabled={!sessionId || !onDeleteSession}
                  onClick={() => setDeleteConfirmOpen(true)}
                >
                  <LuTrash2 size={13} />
                  <Box flex={1}>{translation("deleteSession")}</Box>
                </Menu.Item>
              </DropdownMenu>
            </Flex>
            </Flex>
            {topBarHiddenStart ? <Box css={fadeOverlayInline("left", FADE_INLINE)} /> : null}
            {topBarHiddenEnd ? <Box css={fadeOverlayInline("right", FADE_INLINE)} /> : null}
            </Box>
          </Flex>
          <Box position="relative" flex={1} minH={0} display="flex" flexDirection="column">
            <Box
              ref={scrollContainerRef}
              flex={1}
              minH={0}
              display="flex"
              flexDirection="column-reverse"
              overflowY="auto"
              px={4}
              py={3}
              onScroll={handleScroll}
              css={transcriptPinned ? scrollFade : scrollFadeTopBottom}
              style={{ overflowAnchor: "none", scrollbarGutter: "stable both-edges" }}
            >
              {connectionLost ? (
                // A lost daemon is a state worth naming, and the only one here whose remedy is a single button.
                <motion.div
                  key="disconnected"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={fadeSurfaceTransition}
                  style={{
                    width: "100%",
                    flex: 1,
                    minHeight: 0,
                    display: "flex",
                    flexDirection: "column",
                  }}
                >
                <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
                  <EmptyState.Root>
                    <EmptyState.Content>
                      <EmptyState.Indicator>
                        <LuPlugZap />
                      </EmptyState.Indicator>
                      <VStack gap={1}>
                        <EmptyState.Title>{translation("disconnectedTitle")}</EmptyState.Title>
                        <EmptyState.Description>
                          {translation("disconnectedDescription")}
                        </EmptyState.Description>
                      </VStack>
                      <Button
                        variant="solid"
                        colorPalette="blue"
                        onClick={onReconnect}
                        loading={reconnecting}
                        disabled={reconnecting}
                      >
                        {translation(reconnecting ? "reconnecting" : "reconnect")}
                      </Button>
                    </EmptyState.Content>
                  </EmptyState.Root>
                </Flex>
                </motion.div>
              ) : !sessionTranscriptReady ? (
                <Flex h="100%" />
              ) : historyError ? (
                <motion.div
                  key="history-error"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={fadeSurfaceTransition}
                  style={{
                    width: "100%",
                    flex: 1,
                    minHeight: 0,
                    display: "flex",
                    flexDirection: "column",
                  }}
                >
                <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
                  <EmptyState.Root>
                    <EmptyState.Content>
                      <EmptyState.Indicator>
                        <LuTriangleAlert />
                      </EmptyState.Indicator>
                      <VStack gap={1}>
                        <EmptyState.Title>
                          {translation("loadConversationErrorTitle")}
                        </EmptyState.Title>
                        <EmptyState.Description>
                          {translation("loadConversationErrorDescription")}
                        </EmptyState.Description>
                      </VStack>
                      <Button variant="solid" colorPalette="blue" onClick={reloadHistory}>
                        {translation("retry")}
                      </Button>
                    </EmptyState.Content>
                  </EmptyState.Root>
                </Flex>
                </motion.div>
              ) : (
                // The welcome and the timeline cross-fade out of flow, so sending the first message never flashes.
                // Locations and capabilities are fetched after mount; keep the centred column unmounted until
                // both have settled so the heading does not jump as those sections appear.
                <AnimatePresence mode="popLayout">
                  {messages.length === 0 && !hasInheritedContext ? (
                    <motion.div
                      key="empty"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={fadeSurfaceTransition}
                      style={{
                        width: "100%",
                        flex: 1,
                        minHeight: 0,
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      {/* The same centred column as the transcript, sitting in the middle of whatever room there is. */}
                      {/* One rhythm for the sections, matching the gap the capability sections keep between themselves. */}
                      <Flex
                        direction="column"
                        align="stretch"
                        gap={6}
                        w="full"
                        maxW="80rem"
                        mx="auto"
                        my="auto"
                        py={{ base: 4, md: 6 }}
                      >
                        {/* The blank-conversation state inside a workspace: the prompt, then what it can reach and what the agent can do. */}
                        <Heading
                          as="h2"
                          fontSize="3xl"
                          fontWeight="semibold"
                          textAlign="center"
                          mb={4}
                        >
                          {translation("buildPrompt", { folder: currentCompactionerName })}
                        </Heading>

                        {/* The locations read as a section like the ones under them, with the same icon, heading and list. */}
                        {workspaceLocations.length > 0 && (
                          <Box w="100%" minW={0}>
                            <SectionHeader
                              icon={<CONCEPT_ICONS.environment size={14} />}
                              title={translation("environmentsAvailable")}
                              description={translation("environmentsDescription")}
                            />
                            <Flex align="center" gap={2.5} wrap="wrap">
                              {workspaceLocations.map((location) => (
                                <LocationChip key={location.id} location={location} />
                              ))}
                            </Flex>
                          </Box>
                        )}

                        <AgentSkills
                          card={agentCard ?? null}
                          workingDirectory={workingDirectory}
                          onReady={() => setSkillsReady(true)}
                        />
                      </Flex>
                    </motion.div>
                  ) : (
                    <motion.div
                      key="timeline"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={fadeSurfaceTransition}
                      style={{ width: "100%", flexShrink: 0 }}
                    >
                      {/* Tight enough that a tool line and the prose around it read as one document, with bubbles marking turns. */}
                      <VStack gap={2.5} align="stretch" w="full" maxW="80rem" mx="auto">
                        {hasInheritedContext ? (
                          <Flex align="center" gap={3} py={2} color="fg.muted">
                            <Separator flex={1} />
                            <Flex align="center" gap={1.5} flexShrink={0}>
                              <Text fontSize="xs" whiteSpace="nowrap">
                                {translation("inheritedContextInherited")}
                              </Text>
                              <LuDot size={18} style={{ opacity: 0.7 }} />
                              <Text fontSize="xs" whiteSpace="nowrap">
                                {translation("inheritedContextChildStarts")}
                              </Text>
                            </Flex>
                            <Separator flex={1} />
                          </Flex>
                        ) : null}

                        {isHistoryStreaming ? (
                          <Flex justify="center" py={2} aria-label="Loading older messages">
                            <Spinner boxSize="1em" borderWidth="2px" />
                          </Flex>
                        ) : null}

                        {/* No presence wrapper, deliberately: a transcript row appears when it exists and is gone when it does not. The scroller is a column-reverse flex, so the latest row sits at the scroll origin. */}
                        {renderedTimeline.map((item, itemIndex) => {
                          const isLastItem = itemIndex === renderedTimeline.length - 1;
                          const key = item.kind === "tool_group" ? item.id : item.message.id;
                          const inner =
                            item.kind === "tool_group" ? (
                              <ChatToolGroup messages={item.messages} />
                            ) : (
                              <ChatMessageItem
                                message={item.message}
                                onRetry={
                                  item.message.role === "error"
                                    ? handleRetry
                                    : item.message.role === "compaction" &&
                                        item.message.meta?.status === "failed"
                                      ? compact
                                      : undefined
                                }
                                retrying={item.message.meta?.retrying === true}
                                streaming={isStreaming && isLastItem}
                                onOpenReview={openGoalReview}
                                reviewId={
                                  item.message.role === "user"
                                    ? reviewIdByUserMessage.get(item.message.id)
                                    : undefined
                                }
                              />
                            );
                          // Assistant messages stream, so their wrapper stays stable; complete rows get a single gentle fade.
                          const isAssistantMessage =
                            item.kind === "message" && item.message.role === "assistant";
                          if (isAssistantMessage) {
                            return (
                              <Box key={key} display="flex" flexDirection="column">
                                {inner}
                              </Box>
                            );
                          }
                          return (
                            <FadeIn
                              key={key}
                              animate={animatedKeys.has(key)}
                              style={{ display: "flex", flexDirection: "column" }}
                            >
                              {inner}
                            </FadeIn>
                          );
                        })}
                        {/* A queued message, drawn as itself; each stays a queued card until the session has
                            actually taken it, so the one being handed over is never invisible. */}
                        {queuedMessages.map((message, index) => (
                          <UserMessageCard
                            key={message.id}
                            message={{
                              id: message.id,
                              role: "user",
                              content: message.text,
                              timestamp: "",
                            }}
                            queued={{
                              status: translation(
                                outboxHold === "unreachable"
                                  ? "queuedUnreachable"
                                  : outboxHold === "decision"
                                    ? "queuedForDecision"
                                    : outboxHold === "compaction"
                                      ? "queuedForCompaction"
                                      : "queued",
                              ),
                              failed: outboxHold === "unreachable",
                              onDelete: () => dequeueMessage(index),
                              ...(outboxHold === "unreachable" && index === 0
                                ? { onRetry: retryOutbox, retryLabel: translation("queuedRetry") }
                                : {}),
                            }}
                          />
                        ))}
                        {/* A hand-over the session has taken but not yet drawn back: it is queued, not
                            delivered, so it stays a queued card until the session echoes its row. */}
                        {visibleHandedMessages.map((message) => (
                          <UserMessageCard
                            key={message.id}
                            message={{
                              id: message.id,
                              role: "user",
                              content: message.text,
                              timestamp: "",
                            }}
                            queued={{
                              status: translation("queued"),
                            }}
                          />
                        ))}
                        {/* The daemon's awaiting_model status: one wait row, not a spinning last tool group. */}
                        {awaitingProvider ? (
                          <TranscriptWaitRow
                            key="awaiting-provider"
                            label={translation("sendingRequestToProvider")}
                          />
                        ) : null}
                      </VStack>
                    </motion.div>
                  )}
                </AnimatePresence>
              )}
            </Box>
            {!isAtBottom && timelineMounted && (
              <Button
                variant="outline"
                position="absolute"
                bottom={3}
                left="50%"
                transform="translateX(-50%)"
                zIndex={2}
                bg="bg.subtle"
                color="fg"
                fontWeight="medium"
                px={2}
                onClick={scrollToBottom}
              >
                {/* A fixed icon slot, so spinner and arrow share a box and the label never shifts; the spinner is
                    shrunk inside it because the arrow glyph fills only ~2/3 of its box, so a full-size ring reads too big. */}
                <Flex align="center" justify="center" boxSize="4" flexShrink={0}>
                  {isStreaming ? <Spinner boxSize="3" borderWidth="2px" /> : <LuArrowDown />}
                </Flex>
                {translation(isStreaming ? "jumpToProgress" : "jumpToLatest")}
              </Button>
            )}
          </Box>

          {/* The overlay sits in the same centred column as the messages, with no clipping to slice its shadow. */}
          {/* One owner for the transition, keyed by the request, so two decisions are never on screen at once. */}
          <AnimatePresence mode="wait" initial={false}>
            {pendingPrompt && (
              <FadeIn key={pendingPromptId} seconds={0.15}>
                <Box px={4}>
                  <Box w="full" maxW="80rem" mx="auto">
                    {pendingPrompt.kind === "question" && (
                      <QuestionOverlay
                        question={pendingPrompt.question}
                        onQuestion={handleQuestion}
                        onDismiss={declineQuestion}
                      />
                    )}
                    {pendingPrompt.kind === "permission" && (
                      <PermissionOverlay
                        permission={pendingPrompt.permission}
                        title={pendingPrompt.title}
                        detail={pendingPrompt.detail}
                        detailPaths={pendingPrompt.detailPaths}
                        command={pendingPrompt.command}
                        arguments={pendingPrompt.arguments}
                        onPermission={handlePermission}
                      />
                    )}
                  </Box>
                </Box>
              </FadeIn>
            )}
          </AnimatePresence>
          {/* The composer mirrors the transcript's horizontal geometry, with the scrollbar gutter reserved on both edges. */}
          {/* The bottom padding is the phone's home indicator, and zero everywhere else. */}
          {chatReady && (
            <Box
              px={4}
              pb="var(--safe-bottom, 0px)"
              overflowY="hidden"
              // Never gives up its height, so a growing composer cannot push itself past the bottom of the window.
              flexShrink={0}
              style={{ scrollbarGutter: "stable both-edges" }}
            >
              <Box w="full" maxW="80rem" mx="auto">
                {/* Above the composer, because a session with a goal is one the person typing should see and be able to end. */}
                <AnimatePresence>
                  {sessionTranscriptReady && activeGoal ? (
                    <motion.div
                      key="goal"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={fadeSurfaceTransition}
                    >
                      <GoalBar
                        goal={activeGoal}
                        onClear={handleClearGoal}
                        onResume={activeGoal.status === "parked" ? handleResumeGoal : undefined}
                        onOpenReview={openCurrentReview}
                      />
                    </motion.div>
                  ) : null}
                </AnimatePresence>
                <ChatInput
                  onSend={handleSend}
                  onAbort={abort}
                  isStreaming={isStreaming}
                  disabled={!isConnected || compactionFailed}
                  awaitingDecision={!!pendingPrompt}
                  sessionId={sessionId}
                  initialDraft={initialInputDraft}
                  onDraftChange={handleInputDraftChange}
                  workingDirectory={workingDirectory}
                  directoryAvailable={directoryAvailable}
                  agents={agents}
                  selectedAgent={agent}
                  onAgentChange={onAgentChange}
                  models={models}
                  modelProviders={modelProviders}
                  recentModels={recentModels}
                  agentModel={agentModel}
                  onAgentModelChange={onAgentModelChange}
                  onRetryModels={onRetryModels}
                  permissionMode={effectivePermissionMode}
                  onPermissionModeChange={handlePermissionModeChange}
                  sandboxEnforce={sandboxEnforce}
                  sandboxBackend={sandboxBackend.backend}
                  onSandboxEnforceChange={onSandboxEnforceChange}
                  tokenUsage={tokenUsage}
                  tasks={tasks}
                  onCompact={compact}
                  isCompacting={isCompacting}
                />
              </Box>
            </Box>
          )}
        </Flex>

        {/* The right region tiles every open panel into a resizable grid, flush to the chat with an overlapping handle. */}
        <AnimatePresence initial={false}>
          {sidePanels.length > 0 && (
            <MotionBox
              key="panel-region"
              data-layout="side-panel-region"
              flexShrink={0}
              h="100%"
              w={{ base: "100%", md: `min(${sidePanelWidth}px, 55%)` }}
              minW={{ base: "100%", md: "min(360px, 55%)" }}
              maxW={{ base: "100%", md: "80vw" }}
              pr={{ base: 0, md: 2 }}
              pb={{ base: 0, md: 2 }}
              position={{ base: "absolute", md: "relative" }}
              inset={{ base: 0, md: "auto" }}
              zIndex={{ base: 3, md: "auto" }}
              // The same slide and fade as the history sidebar, mirrored, with only transform and opacity animating.
              initial={{ opacity: 0, x: 24 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 24 }}
              transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
            >
              <Box
                display={{ base: "none", md: "block" }}
                position="absolute"
                top={0}
                bottom={0}
                left={-1}
                w={2}
                cursor="col-resize"
                zIndex={1}
                onPointerDown={startSidePanelResize}
              />
              <PanelTiles gap={8} panels={sidePanels} singlePanelOnMobile />
            </MotionBox>
          )}
        </AnimatePresence>

        <SettingsDialog
          open={settingsOpen}
          onOpenChange={setSettingsOpen}
          section={settingsSection}
          onSectionChange={setSettingsSection}
          workspaceId={workspaceId}
          workingDirectory={workingDirectory}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          agents={agents}
          selectedAgent={agent}
          onAgentChange={onAgentChange}
          livePermissionMode={permissionMode}
          onPermissionModeSaved={handleSettingsPermissionModeSaved}
          liveSandboxEnforce={sandboxEnforce}
          sandboxBackend={sandboxBackend}
          onSandboxEnforceChange={onSandboxEnforceChange}
          liveWorktreeStrategy={worktreeStrategy}
          onWorktreeStrategyChange={onWorktreeStrategyChange}
          onRetryModels={onRetryModels}
        />

        <ConfirmDialog
          open={deleteConfirmOpen}
          onOpenChange={setDeleteConfirmOpen}
          title={translation("deleteSessionConfirmTitle")}
          confirmLabel={translation("delete")}
          confirmIcon={<LuTrash2 size={14} />}
          danger
          onConfirm={() => {
            if (sessionId) onDeleteSession?.(sessionId);
          }}
        >
          {translation("deleteSessionConfirmBody")}
        </ConfirmDialog>
      </Flex>
    </AgentNamesProvider>
  );
}
