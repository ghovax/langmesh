"use client";

// What a session remembers: the findings its work established, and the instructions it was given.

import { Badge, Box, Button, Collapsible, Flex, Spinner, Text, VStack } from "@chakra-ui/react";
import { Fragment, memo, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  LuArchive,
  LuBookMarked,
  LuCircleDashed,
  LuCrosshair,
  LuDot,
  LuFile,
  LuGitBranch,
  LuGitMerge,
  LuCompass,
  LuFlag,
  LuInfo,
  LuLock,
  LuPencil,
  LuTriangleAlert,
} from "react-icons/lu";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { fetchSessionRecord, subscribeEvents, type RecordEntry } from "@/lib/api";
import { swallowed } from "@/lib/swallowed";
import { InlineMarkdown } from "./markdown-content";
import { Tooltip } from "./ui/tooltip";

// A live entry and the versions it grew out of, newest first, so the reader sees one item rather than a pile.
interface Revised {
  entry: RecordEntry;
  earlier: RecordEntry[];
}

/** The record is append-only, so a revision is a new entry naming the old one; only the unreplaced ones are current. */
function current(all: RecordEntry[]): Revised[] {
  const byId = new Map(all.map((entry) => [entry.id, entry]));
  const replaced = new Set(all.flatMap((entry) => entry.supersedes ?? []));
  // Walk each chain back through what it replaced, guarding against a cycle the store cannot rule out.
  const history = (entry: RecordEntry): RecordEntry[] => {
    const seen = new Set<string>([entry.id]);
    const earlier: RecordEntry[] = [];
    let pending = [...(entry.supersedes ?? [])];
    while (pending.length > 0) {
      const id = pending.shift() as string;
      if (seen.has(id)) continue;
      seen.add(id);
      const older = byId.get(id);
      if (!older) continue;
      earlier.push(older);
      pending = pending.concat(older.supersedes ?? []);
    }
    return earlier.sort((one, other) =>
      (other.written_at ?? "").localeCompare(one.written_at ?? ""),
    );
  };
  return (
    all
      .filter((entry) => !replaced.has(entry.id))
      .map((entry) => ({ entry, earlier: history(entry) }))
      // Most recent first: a record spanning months is read from what it has just learned, not from its beginning.
      .sort((one, other) =>
        (other.entry.written_at ?? "").localeCompare(one.entry.written_at ?? ""),
      )
  );
}

/** Merge append-only entries without replacing objects the panel has already rendered. */
function mergeEntries(held: RecordEntry[], found: RecordEntry[]): RecordEntry[] {
  const knownIdentifiers = new Set(held.map((entry) => entry.id));
  const additions = found.filter((entry) => !knownIdentifiers.has(entry.id));
  return additions.length > 0 ? held.concat(additions) : held;
}

// What a revision did to what it replaced, in its own colour and mark, since an entry whose ancestor was wrong reads differently from one merely sharpened.
const REVISION_MARK: Record<string, { icon: typeof LuPencil; tone: string }> = {
  correction: { icon: LuPencil, tone: "orange" },
  refinement: { icon: LuCrosshair, tone: "blue" },
  merge: { icon: LuGitMerge, tone: "purple" },
  retraction: { icon: LuArchive, tone: "gray" },
};

// What kind of thing an entry is, marked so a list is read by shape before it is read word by word.
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

// An entry that no longer governs the work, which is the one thing that moves it down the list.
function retired(entry: RecordEntry): boolean {
  return entry.revision === "retraction" || entry.still_binding === false;
}

// How sure the record is of an entry, which the reader needs before acting on it.
const STANDING_TONE: Record<string, string> = {
  reported: "gray",
  inferred: "orange",
};

interface EntryLabels {
  category: Record<string, string>;
  kind: Record<string, string>;
  standing: Record<string, string>;
  lifted: string;
  revision: Record<string, string>;
}

// The separator between the qualifiers under an entry, drawn rather than typed so it sits on the line.
function Dot() {
  return <LuDot size={16} style={{ flexShrink: 0, opacity: 0.7 }} />;
}

// A qualifier reads as a word on the line, not as a chip: the colour carries the meaning, so a background only adds noise.
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

// Keep current entries scannable and reveal historical entries in full.
function Body({
  entry,
  muted,
  expanded = false,
}: {
  entry: RecordEntry;
  muted?: boolean;
  expanded?: boolean;
}) {
  const claim = entry.claim ?? entry.summary ?? "";
  const cited = entry.evidence || entry.occasion || "";
  return (
    <>
      <Text
        textStyle="xs"
        fontWeight="medium"
        color={muted ? "fg.muted" : undefined}
        truncate={expanded ? undefined : true}
      >
        <InlineMarkdown content={claim} />
      </Text>
      {entry.detail ? (
        <Text textStyle="2xs" color="fg.muted" mt={1} truncate={expanded ? undefined : true}>
          <InlineMarkdown content={entry.detail} />
        </Text>
      ) : null}
      {cited ? (
        // Prose as often as a path, so it is set as prose and the model's own backticks mark what is literal.
        <Text textStyle="2xs" color="fg.subtle" mt={1} truncate={expanded ? undefined : true}>
          <InlineMarkdown content={cited} />
        </Text>
      ) : null}
    </>
  );
}

const Entry = memo(function Entry({ revised, labels }: { revised: Revised; labels: EntryLabels }) {
  const { entry, earlier } = revised;
  const revisionMark = entry.revision ? REVISION_MARK[entry.revision] : undefined;
  const label = entry.category
    ? labels.category[entry.category]
    : entry.kind
      ? labels.kind[entry.kind]
      : "";
  const standing =
    entry.standing && entry.standing !== "verified" ? labels.standing[entry.standing] : "";
  const entryQualifiers = [
    label ? (
      <Qualifier
        key="label"
        mark={ENTRY_MARK[entry.category ?? entry.kind ?? ""]?.icon}
        tone={ENTRY_MARK[entry.category ?? entry.kind ?? ""]?.tone}
      >
        {label}
      </Qualifier>
    ) : null,
    standing ? (
      <Qualifier key="standing" tone={STANDING_TONE[entry.standing ?? ""] ?? "gray"}>
        {standing}
      </Qualifier>
    ) : null,
    entry.still_binding === false ? <Qualifier key="lifted">{labels.lifted}</Qualifier> : null,
    entry.written_at ? (
      <RelativeTime key="learned" date={entry.written_at} textStyle="xs" color="fg.subtle" />
    ) : null,
  ].filter(Boolean);
  const revisionLabel = entry.revision ? labels.revision[entry.revision] : "";
  const RevisionIcon = revisionMark?.icon;
  const revisionQualifier =
    earlier.length > 0 && revisionMark && RevisionIcon && revisionLabel ? (
      <Collapsible.Trigger key="revisions" asChild>
        <Button
          variant="plain"
          px={0}
          h="auto"
          minW={0}
          gap={1}
          textStyle="xs"
          fontWeight="medium"
          css={{ "& svg": { width: "13px", height: "13px" } }}
          colorPalette={revisionMark?.tone ?? "blue"}
        >
          <RevisionIcon />
          {revisionLabel}
        </Button>
      </Collapsible.Trigger>
    ) : null;
  const qualifiers = [...entryQualifiers, revisionQualifier].filter(Boolean);
  const tooltipQualifiers = [
    ...entryQualifiers,
    revisionMark && revisionLabel ? (
      <Qualifier key="revision" mark={revisionMark.icon} tone={revisionMark.tone}>
        {revisionLabel}
      </Qualifier>
    ) : null,
  ].filter(Boolean);
  const tooltipContent = (
    <Box whiteSpace="normal" maxW="360px">
      <Body entry={entry} expanded />
      <Flex align="center" gap={0.5} mt={2} wrap="wrap" color="fg.muted">
        {tooltipQualifiers.map((qualifier, index) => (
          <Fragment key={index}>
            {index > 0 ? <Dot /> : null}
            {qualifier}
          </Fragment>
        ))}
      </Flex>
    </Box>
  );
  return (
    <Collapsible.Root>
      <Box
        borderWidth="1px"
        borderColor="border"
        borderRadius="md"
        px={2}
        py={1.5}
        bg="bg.subtle"
        opacity={retired(entry) ? 0.55 : 1}
      >
        <Tooltip
          content={tooltipContent}
          rich
          openDelay={250}
          closeDelay={60}
          positioning={{ placement: "left" }}
        >
          <Box>
            <Body entry={entry} />
            {/* Wider than the gaps inside the body: these are qualifiers about the entry, not another line of it. */}
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
        <Collapsible.Content>
          <VStack
            align="stretch"
            gap={2.5}
            mt={1.5}
            mb={0.5}
            ps={2}
            borderLeftWidth="2px"
            borderColor="border.emphasized"
          >
            {/* Each earlier version stays one line until hovered, so a long revision history reads as a list, not a wall. */}
            {earlier.map((older) => (
              <Tooltip
                key={older.id}
                content={
                  <Box whiteSpace="normal" maxW="360px">
                    <Body entry={older} expanded />
                  </Box>
                }
                rich
                openDelay={250}
                closeDelay={60}
                positioning={{ placement: "left" }}
              >
                <Box opacity={0.8}>
                  <Body entry={older} muted />
                  {older.written_at ? (
                    <RelativeTime
                      date={older.written_at}
                      display="block"
                      mt={1}
                      textStyle="2xs"
                      color="fg.subtle"
                    />
                  ) : null}
                </Box>
              </Tooltip>
            ))}
          </VStack>
        </Collapsible.Content>
      </Box>
    </Collapsible.Root>
  );
});

export function MemoryPanel({
  sessionId,
  recording = false,
  onClose,
}: {
  sessionId: string | null;
  recording?: boolean;
  onClose?: () => void;
}) {
  const translation = useTranslations("MemoryPanel");
  const [findingEntries, setFindingEntries] = useState<RecordEntry[]>([]);
  const [instructionEntries, setInstructionEntries] = useState<RecordEntry[]>([]);
  const [read, setRead] = useState(false);
  const findings = useMemo(() => current(findingEntries), [findingEntries]);
  const instructions = useMemo(() => current(instructionEntries), [instructionEntries]);

  useEffect(() => {
    if (!sessionId) return;
    return subscribeEvents((event) => {
      const addition = event as {
        type: string;
        session?: string;
        ledger?: "observations" | "directives";
        entry?: RecordEntry;
      };
      if (
        addition.type !== "record_entry_added" ||
        addition.session !== sessionId ||
        !addition.ledger ||
        !addition.entry
      )
        return;
      const appendEntry =
        addition.ledger === "directives" ? setInstructionEntries : setFindingEntries;
      const entry = addition.entry;
      appendEntry((held) => mergeEntries(held, [entry]));
    });
  }, [sessionId]);

  useEffect(() => {
    let cancelled = false;
    async function readRecord() {
      if (!sessionId) {
        setRead(true);
        return;
      }
      try {
        const [established, asked] = await Promise.all([
          fetchSessionRecord(sessionId, "observations", undefined, false),
          fetchSessionRecord(sessionId, "directives", undefined, false),
        ]);
        if (cancelled) return;
        setFindingEntries((held) => mergeEntries(held, established));
        setInstructionEntries((held) => mergeEntries(held, asked));
        setRead(true);
      } catch (caught) {
        if (!cancelled)
          swallowed({ component: "memory-panel", operation: "read the session's record" }, caught);
      }
    }
    void readRecord();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

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
      lifted: translation("lifted"),
      revision: {
        correction: translation("revision.correction"),
        refinement: translation("revision.refinement"),
        merge: translation("revision.merge"),
        retraction: translation("revision.retraction"),
      },
    }),
    [translation],
  );
  // What was asked for comes first: an instruction governs the findings under it.
  const sections: Array<[string, Revised[]]> = [
    [translation("instructions"), instructions],
    [translation("findings"), findings],
  ];

  return (
    <PanelCard>
      <PanelHeader
        icon={recording ? <Spinner size="xs" colorPalette="orange" /> : <LuBookMarked size={14} />}
        title={translation("title")}
        onClose={onClose}
        closeLabel={translation("collapsePanel")}
      />
      <PanelBody pt={1}>
        {findings.length === 0 && instructions.length === 0 ? (
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
                  {entries.map((revised) => (
                    <Entry key={revised.entry.id} revised={revised} labels={labels} />
                  ))}
                </VStack>
              ),
            )}
          </VStack>
        )}
      </PanelBody>
    </PanelCard>
  );
}
