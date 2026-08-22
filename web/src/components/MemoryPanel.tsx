"use client";

// The active workspace's current observational memory: findings and continuing instructions.

import { Alert, Badge, Box, Flex, Text, VStack } from "@chakra-ui/react";
import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  LuBookMarked,
  LuCircleDashed,
  LuCompass,
  LuDot,
  LuFile,
  LuFlag,
  LuGitBranch,
  LuInfo,
  LuLock,
  LuTriangleAlert,
} from "react-icons/lu";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/Panel";
import { FadeSwitch } from "@/components/ui/FadeIn";
import { RelativeTime } from "@/components/ui/RelativeTime";
import {
  fetchObservationRecord,
  fetchSessionRecord,
  subscribeEvents,
  type RecordEntry,
} from "@/lib/api";
import { reportError } from "@/lib/faults";
import { InlineMarkdown } from "./MarkdownContent";
import { MonoList } from "./ui/Display";
import { Tooltip } from "./ui/Tooltip";

const ENTRY_MARK: Record<string, { icon: typeof LuInfo; tone: string }> = {
  fact: { icon: LuInfo, tone: "blue" },
  decision: { icon: LuGitBranch, tone: "purple" },
  constraint: { icon: LuLock, tone: "orange" },
  failure: { icon: LuTriangleAlert, tone: "red" },
  artifact: { icon: LuFile, tone: "gray" },
  open: { icon: LuCircleDashed, tone: "yellow" },
  requirement: { icon: LuFlag, tone: "blue" },
  preference: { icon: LuCompass, tone: "cyan" },
};

const STANDING_TONE: Record<string, string> = { reported: "gray", inferred: "orange" };

interface EntryLabels {
  category: Record<string, string>;
  kind: Record<string, string>;
  standing: Record<string, string>;
}

function Dot() {
  return <LuDot size={16} style={{ flexShrink: 0, opacity: 0.7 }} />;
}

function Qualifier({
  children,
  tone,
  mark: Mark,
}: {
  children: string;
  tone?: string;
  mark?: typeof LuInfo;
}) {
  return (
    <Badge
      size="sm"
      variant="plain"
      px={0}
      minH={0}
      gap={1}
      colorPalette={tone}
      css={{ "& svg": { width: "13px", height: "13px" } }}
    >
      {Mark ? <Mark /> : null}
      {children}
    </Badge>
  );
}

function Body({ entry, expanded = false }: { entry: RecordEntry; expanded?: boolean }) {
  const claim = entry.claim ?? entry.summary ?? "";
  const cited = entry.evidence || entry.occasion || "";
  return (
    <>
      <Text textStyle="xs" fontWeight="medium" truncate={expanded ? undefined : true}>
        <InlineMarkdown content={claim} />
      </Text>
      {entry.detail ? (
        <Text textStyle="2xs" color="fg.muted" mt={1} truncate={expanded ? undefined : true}>
          <InlineMarkdown content={entry.detail} />
        </Text>
      ) : null}
      {expanded && entry.files?.length ? (
        <Box mt={1}>
          <MonoList items={entry.files} fontSize="2xs" />
        </Box>
      ) : null}
      {cited ? (
        <Text textStyle="2xs" color="fg.subtle" mt={1} truncate={expanded ? undefined : true}>
          <InlineMarkdown content={cited} />
        </Text>
      ) : null}
    </>
  );
}

const Entry = memo(function Entry({ entry, labels }: { entry: RecordEntry; labels: EntryLabels }) {
  const type = entry.category ?? entry.kind ?? "";
  const label = entry.category
    ? labels.category[entry.category]
    : entry.kind
      ? labels.kind[entry.kind]
      : "";
  const standing =
    entry.standing && entry.standing !== "verified" ? labels.standing[entry.standing] : "";
  const qualifiers = [
    label ? (
      <Qualifier key="label" mark={ENTRY_MARK[type]?.icon} tone={ENTRY_MARK[type]?.tone}>
        {label}
      </Qualifier>
    ) : null,
    standing ? (
      <Qualifier key="standing" tone={STANDING_TONE[entry.standing ?? ""] ?? "gray"}>
        {standing}
      </Qualifier>
    ) : null,
    entry.updated_at ? (
      <RelativeTime key="learned" date={entry.updated_at} textStyle="xs" color="fg.subtle" />
    ) : null,
  ].filter(Boolean);
  const tooltipContent = (
    <Box whiteSpace="normal" maxW="360px">
      <Body entry={entry} expanded />
      <Flex align="center" gap={0.5} mt={2} wrap="wrap" color="fg.muted">
        {qualifiers.map((qualifier, index) => (
          <Fragment key={index}>
            {index > 0 ? <Dot /> : null}
            {qualifier}
          </Fragment>
        ))}
      </Flex>
    </Box>
  );
  return (
    <Box borderWidth="1px" borderColor="border" borderRadius="md" px={2} py={1.5} bg="bg.subtle">
      <Tooltip
        content={tooltipContent}
        rich
        openDelay={250}
        closeDelay={60}
        positioning={{ placement: "left" }}
      >
        <Box>
          <Body entry={entry} />
          <Flex align="center" gap={0.5} mt={2} wrap="wrap" color="fg.muted">
            {qualifiers.map((qualifier, index) => (
              <Fragment key={index}>
                {index > 0 ? <Dot /> : null}
                {qualifier}
              </Fragment>
            ))}
          </Flex>
        </Box>
      </Tooltip>
    </Box>
  );
});

function newestFirst(entries: RecordEntry[]): RecordEntry[] {
  return [...entries].sort((one, other) =>
    (other.updated_at ?? "").localeCompare(one.updated_at ?? ""),
  );
}

export function MemoryPanel({
  sessionId,
  workingDirectory,
  onClose,
}: {
  sessionId: string | null;
  workingDirectory?: string;
  onClose?: () => void;
}) {
  const translation = useTranslations("MemoryPanel");
  const [findingEntries, setFindingEntries] = useState<RecordEntry[]>([]);
  const [instructionEntries, setInstructionEntries] = useState<RecordEntry[]>([]);
  const [read, setRead] = useState(false);
  const [registryError, setRegistryError] = useState("");
  const [loadError, setLoadError] = useState(false);
  const latestRevision = useRef(-1);
  const latestSnapshotWasEvent = useRef(false);
  const registryPath = useRef("");
  const directoryRef = useRef("");

  useEffect(() => {
    let cancelled = false;
    const directory = workingDirectory ?? "";
    const directoryChanged = directory !== directoryRef.current;
    directoryRef.current = directory;
    if (directoryChanged) {
      latestRevision.current = -1;
      latestSnapshotWasEvent.current = false;
      registryPath.current = "";
      setFindingEntries([]);
      setInstructionEntries([]);
      setRegistryError("");
      setLoadError(false);
      setRead(false);
    }
    function applySnapshot(
      snapshot: {
        entries?: { observations?: RecordEntry[]; directives?: RecordEntry[] };
        revision?: number;
        error?: string;
        metadata?: { path?: string };
      },
      source: "fetch" | "event",
    ) {
      const revision = Number(snapshot.revision ?? 0);
      if (revision < latestRevision.current) return;
      if (
        revision === latestRevision.current &&
        source === "fetch" &&
        latestSnapshotWasEvent.current
      )
        return;
      latestRevision.current = revision;
      latestSnapshotWasEvent.current = source === "event";
      if (snapshot.metadata?.path) registryPath.current = String(snapshot.metadata.path);
      setFindingEntries(newestFirst(snapshot.entries?.observations ?? []));
      setInstructionEntries(newestFirst(snapshot.entries?.directives ?? []));
      setRegistryError(snapshot.error ?? "");
      setLoadError(false);
      setRead(true);
    }
    async function readRecord() {
      if (!sessionId && !directory) {
        setFindingEntries([]);
        setInstructionEntries([]);
        setRegistryError("");
        setLoadError(false);
        setRead(true);
        return;
      }
      // A session handle appearing for the same folder must not blank or refetch the
      // registry the panel is already showing.
      if (sessionId && registryPath.current && !directoryChanged) return;
      try {
        const snapshot = sessionId
          ? await fetchSessionRecord(sessionId)
          : await fetchObservationRecord(directory);
        if (cancelled) return;
        applySnapshot(snapshot, "fetch");
      } catch (caught) {
        if (!cancelled) {
          setLoadError(true);
          setRead(true);
          reportError({ component: "memory-panel", operation: "read the session's record" }, caught);
        }
      }
    }
    void readRecord();
    const unsubscribe = subscribeEvents((event) => {
      const change = event as {
        type: string;
        sessions?: string[];
        entries?: { observations?: RecordEntry[]; directives?: RecordEntry[] };
        revision?: number;
        error?: string;
        metadata?: { path?: string };
      };
      if (
        change.type !== "observation_registry_changed" ||
        (!(sessionId && change.sessions?.includes(sessionId)) &&
          change.metadata?.path !== registryPath.current)
      )
        return;
      applySnapshot(change, "event");
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [sessionId, workingDirectory]);

  const labels: EntryLabels = useMemo(
    () => ({
      category: {
        fact: translation("category.fact"),
        decision: translation("category.decision"),
        constraint: translation("category.constraint"),
        failure: translation("category.failure"),
        artifact: translation("category.artifact"),
        open: translation("category.open"),
      },
      kind: {
        requirement: translation("kind.requirement"),
        preference: translation("kind.preference"),
      },
      standing: {
        reported: translation("standing.reported"),
        inferred: translation("standing.inferred"),
      },
    }),
    [translation],
  );
  const sections: Array<[string, RecordEntry[]]> = [
    [translation("instructions"), instructionEntries],
    [translation("findings"), findingEntries],
  ];

  return (
    <PanelCard>
      <PanelHeader
        icon={<LuBookMarked size={14} />}
        title={translation("title")}
        onClose={onClose}
        closeLabel={translation("collapsePanel")}
      />
      <PanelBody pt={1}>
        <Text textStyle="xs" color="fg.subtle" mb={2}>
          {translation("description")}
        </Text>
        {loadError ? (
          <Box
            borderWidth="1px"
            borderColor="red.subtle"
            borderRadius="md"
            px={2}
            py={1.5}
            mb={2}
            bg="red.subtle"
          >
            <Text textStyle="xs" color="red.fg" fontWeight="medium">
              {translation("loadError")}
            </Text>
          </Box>
        ) : null}
        {registryError ? (
          <Alert.Root status="error" size="sm" borderRadius="md" alignItems="center" mb={2}>
            <Alert.Indicator />
            <Alert.Content flex={1} minW={0}>
              <Alert.Description fontSize="xs">{translation("registryError")}</Alert.Description>
            </Alert.Content>
          </Alert.Root>
        ) : null}
        <FadeSwitch
          childKey={
            findingEntries.length === 0 && instructionEntries.length === 0
              ? read
                ? "empty"
                : "loading"
              : "entries"
          }
        >
          {findingEntries.length === 0 && instructionEntries.length === 0 ? (
            <PanelEmptyState
              icon={<LuBookMarked />}
              title={translation(read ? "emptyTitle" : "loading")}
              description={translation("emptyDescription")}
            />
          ) : (
            <VStack align="stretch" gap={2.5}>
              {sections.map(([heading, entries]) =>
                entries.length === 0 ? null : (
                  <VStack key={heading} align="stretch" gap={2}>
                    <Text textStyle="sectionLabel">{heading}</Text>
                    {entries.map((entry) => (
                      <Entry
                        key={`${entry.id}:${entry.updated_at ?? ""}`}
                        entry={entry}
                        labels={labels}
                      />
                    ))}
                  </VStack>
                ),
              )}
            </VStack>
          )}
        </FadeSwitch>
      </PanelBody>
    </PanelCard>
  );
}
