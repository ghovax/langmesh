"use client";

// The chat-history sidebar as a self-contained unit: workspaces, a new-session row, and their sessions.

import {
  Alert,
  Box,
  Button,
  Flex,
  IconButton,
  Input,
  Kbd,
  Menu,
  Text,
  VStack,
} from "@chakra-ui/react";
import { reportError } from "@/lib/faults";
import { useTranslations } from "next-intl";
import { useLocale } from "@/lib/i18n/locale-provider";
import { memo, useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import {
  LuArrowDownUp,
  LuChevronDown,
  LuClock,
  LuEllipsis,
  LuFolderOpen,
  LuFolderPlus,
  LuSearch,
  LuSettings,
  LuSquarePen,
  LuTrash2,
} from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { LangMeshMark } from "@/components/ui/LangmeshMark";
import { DropdownMenu, MenuOption } from "@/components/ui/Menu";
import { PanelBody, PanelCard } from "@/components/ui/Panel";
import { FadeSwitch } from "@/components/ui/FadeIn";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  deleteWorkspace,
  fetchAllWorkspaces,
  fetchDaemonTargets,
  listSshHosts,
  subscribeEvents,
  type AgentSummary,
  type FederatedWorkspace,
  type SshHost,
} from "@/lib/api";
import { locationTargetAddress, workspaceLabel } from "./LocationStatus";
import { NewScheduleDialog } from "./NewScheduleDialog";
import { NewWorkspaceDialog } from "./NewWorkspaceDialog";
import { SessionRow, sessionIdentity, type SessionEntry } from "./SessionRow";
import { InlineField } from "./ui/Display";
import { TreeRow } from "./ui/TreeRow";
import { toaster } from "./ui/Toaster";
import { errorMessage } from "@/lib/errors";

// A session row, its status vocabulary and its hover card live together, shared with the tree panel.

// The workspace hover card follows the Git bar's shape, which is already this interface's vocabulary.

function WorkspaceHoverCard({
  label,
  workspace,
  sessionCount,
}: {
  label: string;
  workspace: FederatedWorkspace;
  sessionCount: number;
}) {
  const translation = useTranslations("SessionsSidebar");
  const locations = workspace.locations ?? [];
  return (
    <Box maxW="320px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuFolderOpen size={12} />
        <Text fontWeight="semibold" truncate>
          {label}
        </Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1}>
        {/* Every location, since a workspace reaching two machines is precisely the one whose card is worth opening. */}
        {locations.map((location, index) => (
          <InlineField key={index} label={index === 0 ? translation("fieldLocation") : ""}>
            <Text fontFamily="mono" wordBreak="break-all">
              {locationTargetAddress(location)}
            </Text>
          </InlineField>
        ))}
        <InlineField label={translation("fieldConversations")}>
          <Text>{sessionCount}</Text>
        </InlineField>
        <InlineField label={translation("fieldWorkspace")}>
          <Text fontFamily="mono" wordBreak="break-all" color="fg.muted">
            {workspace.id}
          </Text>
        </InlineField>
      </Flex>
    </Box>
  );
}

export type SessionSort = "recent" | "active";

// Row geometry shared by every row here, so their glyphs, text and left edge line up on one grid.
const ROW_MINIMUM_H = "30px";
// Just wide enough to hold the row glyph centred, so titles hug the pill's left edge.
const LEADING_SLOT = "14px";

function workspaceTitle(
  workspace: FederatedWorkspace,
  locale: string,
  untitled: string,
): string {
  return workspaceLabel(workspace.locations, locale, untitled);
}

// Isolated from the search field so a keystroke does not rescan titles or reconcile the tree on the urgent path.
const WorkspaceSessionTree = memo(function WorkspaceSessionTree({
  search,
  sessions,
  sessionsLoaded,
  workspaces,
  activeSessionId,
  unseenCompletions,
  currentWorkspaceId,
  currentDaemonId,
  agents,
  workspaceOpenOverrides,
  onWorkspaceOpenOverridesChange,
  onSwitchWorkspace,
  onOpenWorkspaceSettings,
  onResume,
  onRequestDelete,
  onRequestWorkspaceDelete,
}: {
  search: string;
  sessions: SessionEntry[];
  sessionsLoaded: boolean;
  workspaces: FederatedWorkspace[];
  activeSessionId: string | null;
  unseenCompletions: Set<string>;
  currentWorkspaceId: string;
  currentDaemonId: string;
  agents: AgentSummary[];
  workspaceOpenOverrides: Record<string, boolean>;
  onWorkspaceOpenOverridesChange: (
    update: (current: Record<string, boolean>) => Record<string, boolean>,
  ) => void;
  onSwitchWorkspace: (workspaceId: string, daemonId: string) => void;
  onOpenWorkspaceSettings: (workspaceId: string) => void;
  onResume: (entry: SessionEntry) => void;
  onRequestDelete: (entry: SessionEntry) => void;
  onRequestWorkspaceDelete: (workspace: FederatedWorkspace) => void;
}) {
  const translation = useTranslations("SessionsSidebar");
  const { locale } = useLocale();
  const searchQuery = search.trim().toLowerCase();
  const shownSessions = useMemo(
    () =>
      searchQuery
        ? sessions.filter((entry) => (entry.title || "").toLowerCase().includes(searchQuery))
        : sessions,
    [sessions, searchQuery],
  );

  // Grouped once per change rather than per render, since a scan per workspace is what every keystroke and
  // every folder toggle would otherwise pay for.
  const visibleWorkspaces = useMemo(() => {
    const byWorkspace = new Map<string, SessionEntry[]>();
    for (const session of shownSessions) {
      // Only the conversations you started; a session a session created is listed in the delegated panel.
      if (session.parentSessionId) continue;
      const key = `${session.daemonId || "local"}:${session.workspaceId ?? ""}`;
      const held = byWorkspace.get(key);
      if (held) held.push(session);
      else byWorkspace.set(key, [session]);
    }
    return workspaces
      .map((workspace) => ({
        workspace,
        sessions: byWorkspace.get(`${workspace.daemonId}:${workspace.id}`) ?? [],
      }))
      .filter(({ sessions: workspaceSessions }) => !searchQuery || workspaceSessions.length > 0);
  }, [workspaces, shownSessions, searchQuery]);

  const daemonGroups = useMemo(() => {
    const groups: {
      id: string;
      name: string;
      remote: boolean;
      items: typeof visibleWorkspaces;
    }[] = [];
    const index = new Map<string, number>();
    for (const item of visibleWorkspaces) {
      const id = item.workspace.daemonId;
      const existing = index.get(id);
      if (existing === undefined) {
        index.set(id, groups.length);
        groups.push({
          id,
          name: item.workspace.remote
            ? item.workspace.daemonName || translation("remote")
            : translation("thisMachine"),
          remote: item.workspace.remote,
          items: [item],
        });
      } else {
        groups[existing].items.push(item);
      }
    }
    return groups;
  }, [visibleWorkspaces, translation]);

  const showDaemonHeaders = daemonGroups.length > 1 || daemonGroups.some((group) => group.remote);

  if (!sessionsLoaded || workspaces.length === 0) return null;
  if (visibleWorkspaces.length === 0) {
    // The same alert the settings panel uses, since a search that found nothing is a state rather than a caption.
    return (
      <Alert.Root status="info" size="sm" borderRadius="md" my={2} alignItems="center">
        <Alert.Indicator />
        <Alert.Content minW={0}>
          <Alert.Description fontSize="xs">
            {translation("noMatches", { query: search })}
          </Alert.Description>
        </Alert.Content>
      </Alert.Root>
    );
  }

  return (
    <VStack gap={1} align="stretch">
      {daemonGroups.map((group) => (
        <Box key={group.id}>
          {showDaemonHeaders ? (
            <Text textStyle="sectionLabel" color="fg.muted" px={2} pt={2} pb={1}>
              {group.name}
            </Text>
          ) : null}
          {group.items.map(({ workspace, sessions: workspaceSessions }) => {
        const workspaceKey = `${workspace.daemonId}:${workspace.id}`;
        const currentKey = `${currentDaemonId}:${currentWorkspaceId}`;
        const label = workspaceTitle(workspace, locale, translation("untitledWorkspace"));
        // Keyed by the workspace alone: including the search text made every keystroke discard the choice.
        const workspaceOpen =
          workspaceOpenOverrides[workspaceKey] ??
          (searchQuery ? workspaceSessions.length > 0 : workspaceKey === currentKey);
        const tooltipContent = (
          <WorkspaceHoverCard
            label={label}
            workspace={workspace}
            sessionCount={workspaceSessions.length}
          />
        );
        const workspaceActions = (
          <Box>
            <DropdownMenu
              trigger={
                <IconButton
                  aria-label={translation("workspaceOptions")}
                  variant="plain"
                  boxSize={5}
                  color="fg.subtle"
                  _hover={{ bg: "transparent", color: "fg" }}
                  _active={{ bg: "transparent" }}
                  _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
                  css={{
                    "&[data-state=open]": {
                      background: "transparent",
                      color: "var(--chakra-colors-fg)",
                    },
                  }}
                >
                  <LuEllipsis size={13} />
                </IconButton>
              }
              minW="180px"
              positioning={{ placement: "bottom-end" }}
            >
              <MenuOption
                value="settings"
                icon={<LuSettings size={14} />}
                onClick={() => onOpenWorkspaceSettings(workspace.id)}
              >
                {translation("workspaceSettings")}
              </MenuOption>
              <Menu.Item
                value="delete-workspace"
                color="red.fg"
                disabled={workspaces.length <= 1}
                onClick={() => onRequestWorkspaceDelete(workspace)}
              >
                <LuTrash2 size={14} />
                <Box flex={1}>{translation("deleteWorkspace")}</Box>
              </Menu.Item>
            </DropdownMenu>
          </Box>
        );

        return (
          <TreeRow
            key={workspaceKey}
            disclosure={workspaceOpen ? "open" : "closed"}
            disclosureLabel={
              workspaceOpen ? translation("hideWorkspace") : translation("showWorkspace")
            }
            // Disclosure only shows what is inside; the name is what chooses the workspace, and choosing
            // one navigates the page and rebuilds the conversation, which is no price for a chevron.
            onDisclosureChange={(nextOpen) => {
              onWorkspaceOpenOverridesChange((current) => ({
                ...current,
                [workspaceKey]: nextOpen,
              }));
            }}
            onActivate={() => {
              // Switching must not collapse the workspace that was open: keep it unless the person closed it by hand.
              if (workspaceKey !== currentKey && currentWorkspaceId) {
                onWorkspaceOpenOverridesChange((current) =>
                  current[currentKey] === false
                    ? current
                    : { ...current, [currentKey]: true },
                );
              }
              onSwitchWorkspace(workspace.id, workspace.daemonId);
            }}
            glyph={<LuFolderOpen size={13} />}
            label={
              <Tooltip
                content={tooltipContent}
                rich
                openDelay={350}
                positioning={{ placement: "right" }}
              >
                <Box minW={0} w="full">
                  <Text textStyle="xs" truncate>
                    {label}
                  </Text>
                </Box>
              </Tooltip>
            }
            actions={workspaceActions}
          >
            {/* Nothing at all for a workspace with no conversations, so the chevron goes with the absent children. */}
            {workspaceSessions.length > 0 ? (
              <VStack gap={1} align="stretch">
                {workspaceSessions.map((entry) => (
                  <SessionRow
                    key={sessionIdentity(entry)}
                    entry={entry}
                    agents={agents}
                    isActive={
                      sessionIdentity(entry) === `${currentDaemonId}:${activeSessionId ?? ""}`
                    }
                    unseenCompletions={unseenCompletions}
                    onResume={onResume}
                    onRequestDelete={onRequestDelete}
                  />
                ))}
              </VStack>
            ) : null}
          </TreeRow>
        );
          })}
        </Box>
      ))}
    </VStack>
  );
});

export function SessionsSidebar({
  sessions,
  sessionsLoaded,
  activeSessionId,
  sessionSort,
  onSessionSortChange,
  unseenCompletions,
  currentWorkspaceId,
  currentDaemonId,
  onSwitchWorkspace,
  onOpenWorkspaceSettings,
  onNewChat,
  onResume,
  onDeleteSession,
  agents,
}: {
  sessions: SessionEntry[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  sessionSort: SessionSort;
  onSessionSortChange: (sort: SessionSort) => void;
  unseenCompletions: Set<string>;
  currentWorkspaceId: string;
  currentDaemonId: string;
  onSwitchWorkspace: (workspaceId: string, daemonId: string) => void;
  onOpenWorkspaceSettings: (workspaceId: string) => void;
  onNewChat: () => void;
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
  // The profiles a schedule can be given, passed down because the parent already holds the list.
  agents: AgentSummary[];
}) {
  const translation = useTranslations("SessionsSidebar");
  const { locale } = useLocale();
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);
  const [pendingWorkspaceDelete, setPendingWorkspaceDelete] = useState<FederatedWorkspace | null>(
    null,
  );
  const [workspaces, setWorkspaces] = useState<FederatedWorkspace[]>([]);
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [sshHostsLoaded, setSshHostsLoaded] = useState(false);
  const [newWorkspaceOpen, setNewWorkspaceOpen] = useState(false);
  const [newScheduleOpen, setNewScheduleOpen] = useState(false);
  const [workspaceOpenOverrides, setWorkspaceOpenOverrides] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState("");
  // The field stays on the urgent path; title matching and the tree reconcile as a deferred update.
  const deferredSearch = useDeferredValue(search);

  const refreshWorkspaces = useCallback(() => {
    fetchAllWorkspaces()
      .then(setWorkspaces)
      .catch((caught) =>
        reportError({ component: "sessions-sidebar", operation: "list the workspaces" }, caught),
      );
  }, []);

  useEffect(() => {
    let cancelled = false;
    const refreshSshHosts = () => {
      listSshHosts()
        .then((nextHosts) => {
          if (cancelled) return;
          setSshHosts(nextHosts);
          setSshHostsLoaded(true);
        })
        .catch(() => {
          if (!cancelled) setSshHostsLoaded(true);
        });
    };
    refreshWorkspaces();
    refreshSshHosts();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "workspaces_changed") refreshWorkspaces();
      if (event.type === "hosts_changed") refreshSshHosts();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [refreshWorkspaces]);

  async function confirmWorkspaceDelete() {
    if (!pendingWorkspaceDelete) return;
    const deletedWorkspaceId = pendingWorkspaceDelete.id;
    const deletedDaemonId = pendingWorkspaceDelete.daemonId;
    try {
      const targets = await fetchDaemonTargets();
      const target = targets.find((candidate) => candidate.id === deletedDaemonId);
      await deleteWorkspace(
        deletedWorkspaceId,
        target ? { apiBase: target.endpoint, token: target.token } : undefined,
      );
      const remainingWorkspaces = workspaces.filter(
        (workspace) =>
          !(workspace.id === deletedWorkspaceId && workspace.daemonId === deletedDaemonId),
      );
      setWorkspaces(remainingWorkspaces);
      if (
        deletedWorkspaceId === currentWorkspaceId &&
        deletedDaemonId === currentDaemonId &&
        remainingWorkspaces[0]
      ) {
        onSwitchWorkspace(remainingWorkspaces[0].id, remainingWorkspaces[0].daemonId);
      }
    } catch (error) {
      toaster.create({
        type: "error",
        title: translation("deleteWorkspaceError"),
        description: errorMessage(error),
        closable: true,
      });
    }
  }

  function workspaceName(workspace: FederatedWorkspace): string {
    return workspaceTitle(workspace, locale, translation("untitledWorkspace"));
  }

  return (
    <PanelCard flex={1}>
      <Flex align="center" gap={2} px={3} pt={3} pb={2} flexShrink={0}>
        <LangMeshMark size="26px" style={{ flexShrink: 0 }} />
        <Text
          fontFamily="var(--font-display)"
          fontSize="2xl"
          lineHeight="1"
          fontWeight="bold"
          letterSpacing="tight"
        >
          LangMesh
        </Text>
      </Flex>

      {/* New session reads as the first row of the list rather than a separate button, with its shortcut on hover. */}
      <Box px={2} pt={1} flexShrink={0} pb={1}>
        <Button
          type="button"
          variant="subtle"
          colorPalette="blue"
          w="full"
          minH={ROW_MINIMUM_H}
          gap={1.5}
          px={2}
          justifyContent="flex-start"
          textAlign="left"
          disabled={activeSessionId === null}
          css={{ "& [data-kbd-hint]": { opacity: 0 }, "&:hover [data-kbd-hint]": { opacity: 1 } }}
          onClick={onNewChat}
        >
          <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
            <LuSquarePen size={14} />
          </Flex>
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">
            {translation("newConversation")}
          </Text>
          {/* The semantic keyboard-key component in its plain variant, so it reads as a hint rather than a keycap. */}
          <Kbd
            data-kbd-hint
            variant="plain"
            fontFamily="var(--app-font-sans)"
            fontSize="xs"
            color="blue.fg"
            transition="opacity 0.12s"
            flexShrink={0}
          >
            ⌘N
          </Kbd>
        </Button>
      </Box>

      <Box px={2} flexShrink={0} pb={1}>
        <Button
          type="button"
          variant="outline"
          w="full"
          minH={ROW_MINIMUM_H}
          gap={1.5}
          px={2}
          justifyContent="flex-start"
          textAlign="left"
          onClick={() => setNewWorkspaceOpen(true)}
        >
          <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
            <LuFolderPlus size={14} />
          </Flex>
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">
            {translation("newWorkspace")}
          </Text>
        </Button>
      </Box>

      {/* A schedule is something you make, so it sits with the workspaces and conversations rather than in Settings. */}
      {currentWorkspaceId ? (
        <Box px={2} flexShrink={0} pb={1}>
          <Button
            type="button"
            variant="outline"
            w="full"
            minH={ROW_MINIMUM_H}
            gap={1.5}
            px={2}
            justifyContent="flex-start"
            textAlign="left"
            onClick={() => setNewScheduleOpen(true)}
          >
            <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
              <LuClock size={14} />
            </Flex>
            <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">
              {translation("newSchedule")}
            </Text>
          </Button>
        </Box>
      ) : null}

      {/* Filter the list by title — the same field treatment as the settings search. */}
      <Box px={2} flexShrink={0} pb={1}>
        <Flex
          align="center"
          gap={2}
          h={8}
          px={2}
          borderRadius="md"
          bg="bg.subtle"
          borderWidth="1px"
          borderColor="border.muted"
          _focusWithin={{ borderColor: "border.emphasized" }}
        >
          <Box color="fg.muted" flexShrink={0} display="flex" alignItems="center">
            <LuSearch size={14} />
          </Box>
          <Input
            border="none"
            size="xs"
            h="full"
            px={0}
            placeholder={translation("searchPlaceholder")}
            aria-label={translation("searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            _focusVisible={{ boxShadow: "none", outline: "none" }}
          />
        </Flex>
      </Box>

      <PanelBody pt={1}>
        <Flex align="center" gap={1.5} mb={1} color="fg.muted">
          <Text textStyle="sectionLabel" flex={1}>
            {translation("workspaces")}
          </Text>
          <Box>
            <DropdownMenu
              trigger={
                <Button
                  aria-label={translation("sortSessions")}
                  variant="ghost"
                  color="fg.muted"
                  textStyle="fieldLabel"
                  gap={1}
                  size="2xs"
                  // The menu returns focus here on close, so the ring is swapped for a background that reads either way.
                  _focusVisible={{ outline: "none", boxShadow: "none", bg: "bg.subtle" }}
                >
                  <LuArrowDownUp size={12} />
                  {sessionSort === "active" ? translation("activeFirst") : translation("newest")}
                  <LuChevronDown size={12} />
                </Button>
              }
              minW="170px"
              positioning={{ placement: "bottom-end" }}
            >
              <Menu.ItemGroup>
                <Menu.ItemGroupLabel>{translation("sortBy")}</Menu.ItemGroupLabel>
                <MenuOption
                  value="recent"
                  selected={sessionSort === "recent"}
                  onClick={() => onSessionSortChange("recent")}
                >
                  {translation("newestFirst")}
                </MenuOption>
                <MenuOption
                  value="active"
                  selected={sessionSort === "active"}
                  onClick={() => onSessionSortChange("active")}
                >
                  {translation("activeFirst")}
                </MenuOption>
              </Menu.ItemGroup>
            </DropdownMenu>
          </Box>
        </Flex>
        <FadeSwitch
          childKey={sessionsLoaded && workspaces.length > 0 ? "tree" : "pending"}
        >
        <WorkspaceSessionTree
          search={deferredSearch}
          sessions={sessions}
          sessionsLoaded={sessionsLoaded}
          workspaces={workspaces}
          activeSessionId={activeSessionId}
          unseenCompletions={unseenCompletions}
          currentWorkspaceId={currentWorkspaceId}
          currentDaemonId={currentDaemonId}
          agents={agents}
          workspaceOpenOverrides={workspaceOpenOverrides}
          onWorkspaceOpenOverridesChange={setWorkspaceOpenOverrides}
          onSwitchWorkspace={onSwitchWorkspace}
          onOpenWorkspaceSettings={onOpenWorkspaceSettings}
          onResume={onResume}
          onRequestDelete={setPendingDelete}
          onRequestWorkspaceDelete={setPendingWorkspaceDelete}
        />
        </FadeSwitch>
      </PanelBody>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={translation("deleteTitle")}
        confirmLabel={translation("deleteConfirm")}
        danger
        onConfirm={() => {
          if (pendingDelete) onDeleteSession(pendingDelete);
        }}
      >
        {translation("deleteBody", {
          title: pendingDelete?.title || translation("untitledConversation"),
        })}
      </ConfirmDialog>

      {newWorkspaceOpen ? (
        <NewWorkspaceDialog
          open
          hosts={sshHosts}
          hostsLoaded={sshHostsLoaded}
          onOpenChange={setNewWorkspaceOpen}
          onCreated={(workspace) => {
            const located = {
              ...workspace,
              daemonId: currentDaemonId,
              daemonName: "",
              remote: currentDaemonId !== "local",
            };
            setWorkspaces((current) => [
              located,
              ...current.filter(
                (entry) => !(entry.id === workspace.id && entry.daemonId === currentDaemonId),
              ),
            ]);
            onSwitchWorkspace(workspace.id, currentDaemonId);
          }}
        />
      ) : null}

      <NewScheduleDialog
        workspaceId={currentWorkspaceId}
        agents={agents}
        open={newScheduleOpen}
        onOpenChange={setNewScheduleOpen}
      />

      <ConfirmDialog
        open={pendingWorkspaceDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingWorkspaceDelete(null);
        }}
        title={translation("deleteWorkspaceTitle")}
        confirmLabel={translation("deleteConfirm")}
        danger
        onConfirm={() => void confirmWorkspaceDelete()}
      >
        {translation("deleteWorkspaceBody", {
          workspace: pendingWorkspaceDelete ? workspaceName(pendingWorkspaceDelete) : "",
        })}
      </ConfirmDialog>
    </PanelCard>
  );
}
