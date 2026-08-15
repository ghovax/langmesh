"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { memo, useMemo, useState, type ReactNode } from "react";
import { LuBrain } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/glyphs";
import { STATUS_ICON, STATUS_PALETTE, type StatusKind } from "@/lib/status";
import { RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import { Pill } from "./ui/pill";
import { DisclosureRow } from "./ui/disclosure-row";
import { ActivitySpinner } from "./ui/activity-icon";
import type { ToolEvent } from "@/lib/tool-event";
import { hasBackgroundJobId, toolStatus } from "@/lib/tool-event";
import {
  ToolCall,
  ToolCallDetail,
  ToolLocationBadge,
  ToolAccessBadges,
  collapsedHeadingLocation,
  toolCallDetail,
} from "./tool-call";

// The grouped, collapsible run of contiguous tool calls, and the single source of truth for how a batch reads.

// Tally tools by name, splitting each type's count into successful and failed calls, preserving first-seen order.
function tallyToolOutcomes(tools: ToolEvent[]): { name: string; total: number; failed: number }[] {
  const order: string[] = [];
  const total = new Map<string, number>();
  const failed = new Map<string, number>();
  for (const tool of tools) {
    const name = tool.name || "unknown";
    if (!total.has(name)) order.push(name);
    total.set(name, (total.get(name) ?? 0) + 1);
    if (toolStatus(tool.status) === "failed") failed.set(name, (failed.get(name) ?? 0) + 1);
  }
  return order.map((name) => ({
    name,
    total: total.get(name) ?? 0,
    failed: failed.get(name) ?? 0,
  }));
}

// One tally chip in the heading, carrying both the icon and its count so each reads as a single unit.
function TallyBadge({
  icon,
  count,
  colorPalette = "gray",
  title,
  alwaysShowCount = false,
}: {
  icon: ReactNode;
  count: number;
  colorPalette?: string;
  title?: string;
  alwaysShowCount?: boolean;
}) {
  return (
    <Pill colorPalette={colorPalette} title={title} icon={icon}>
      {alwaysShowCount || count > 1 ? <RollingNumber value={count} /> : null}
    </Pill>
  );
}

// Map a tool's icon colour to a palette, so each tally badge carries that tool's own accent.
function paletteFromIconColor(iconColor: string): string {
  return iconColor.endsWith(".fg") ? iconColor.slice(0, -3) : "gray";
}

interface ToolGroupProps {
  tools: ToolEvent[];
  // Keeps the group expanded after its calls complete, so the latest one stays open until the answer arrives.
  keepOpen?: boolean;
  pendingLabel?: string;
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  keepOpen = false,
  pendingLabel,
}: ToolGroupProps) {
  const translation = useTranslations("ToolGroup");
  const backgroundCount = tools.filter(
    (tool) => toolStatus(tool.status) === "running" && hasBackgroundJobId(tool.result),
  ).length;
  const runningCount =
    tools.filter((tool) => toolStatus(tool.status) === "running").length - backgroundCount;
  const inputRequired = tools.some((tool) => toolStatus(tool.status) === "input_required");
  const active = runningCount > 0 || backgroundCount > 0 || inputRequired || keepOpen;
  const showActivitySpinner = runningCount > 0 || (keepOpen && !inputRequired);
  // Tri-state, so the group can be toggled either way from its default: null follows the default.
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? false;

  // The heading icon is the latest call's mark; the tally badges carry the per-type counts, split
  // into successful and failed calls so a failure shows as that type's own red counter.
  const tally = useMemo(() => tallyToolOutcomes(tools), [tools]);
  const latestTool = tools[tools.length - 1];
  const headingDisplay = latestTool
    ? getToolCallDisplay(latestTool.name, latestTool.arguments)
    : null;
  const HeadingIcon = headingDisplay?.icon ?? LuBrain;
  const headingIconColor = headingDisplay?.iconColor ?? "purple.fg";
  // Badge the collapsed heading when the batch touched a single remote place, since local is the implied default.
  const groupLocation = useMemo(
    () => collapsedHeadingLocation(tools.map((tool) => tool.arguments)),
    [tools],
  );
  // A group of one skips the per-call line and opens straight onto that call's detail.
  const soleTool = tools.length === 1 ? tools[0] : null;
  const soleDetail = soleTool
    ? toolCallDetail(soleTool.name, soleTool.arguments, soleTool.result, soleTool.status)
    : null;
  // Any call can be opened, including a single one, since the row carries the label rather than the arguments.
  const interactive = soleTool ? !!soleDetail?.collapsible : tools.length > 0;

  // Status chips surface only the states needing attention; running and completed calls speak for themselves.
  const statusChips = [
    backgroundCount > 0 && {
      kind: "background" as StatusKind,
      count: backgroundCount,
      title: translation("backgroundCount", { count: backgroundCount }),
    },
  ].filter((chip): chip is { kind: StatusKind; count: number; title: string } => Boolean(chip));

  const titleSlot = (
    <Box minW={0} className={active ? "running-title-shimmer" : undefined}>
      <Text
        textStyle="sm"
        fontWeight="normal"
        whiteSpace="nowrap"
        overflow="hidden"
        textOverflow="ellipsis"
      >
        {latestTool ? (
          <ToolCallLabel name={latestTool.name} args={latestTool.arguments} ready />
        ) : (
          (pendingLabel ?? translation("thinking"))
        )}
      </Text>
    </Box>
  );

  // The heading's chip cluster: tallies, file changes, the remote badge and status chips, all animated.
  const hasBadges = tally.length > 0 || statusChips.length > 0 || !!groupLocation || !!soleTool;
  const badgeSlot = (
    <>
      {/* The write and access markers of a lone call move up to the heading rather than disappearing with its line. */}
      {soleTool ? <ToolAccessBadges name={soleTool.name} arguments={soleTool.arguments} /> : null}
      {!soleTool ? (
        <AnimatePresence initial={false}>
          {tally.map(({ name, total, failed }) => {
            const display = getToolCallDisplay(name, undefined);
            const ToolIcon = display.icon;
            const successful = total - failed;
            // A type reads as one badge unless it carries failures: then it splits into its own
            // green successful counter and red failed counter, so the batch sums to the total.
            const badges = [
              failed === 0 ? (
                <TallyBadge
                  key={name}
                  title={display.label}
                  count={total}
                  colorPalette={paletteFromIconColor(display.iconColor)}
                  icon={<ToolIcon />}
                  alwaysShowCount
                />
              ) : successful > 0 ? (
                <TallyBadge
                  key={`${name}-ok`}
                  title={display.label}
                  count={successful}
                  colorPalette="green"
                  icon={<ToolIcon />}
                  alwaysShowCount
                />
              ) : null,
              failed > 0 ? (
                <TallyBadge
                  key={`${name}-failed`}
                  title={translation("failedCount", { count: failed })}
                  count={failed}
                  colorPalette="red"
                  icon={<ToolIcon />}
                  alwaysShowCount
                />
              ) : null,
            ];
            return (
              <motion.div
                key={name}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.12, ease: "easeOut" }}
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
              >
                {badges}
              </motion.div>
            );
          })}
        </AnimatePresence>
      ) : null}
      <ToolLocationBadge arguments={groupLocation} />
      <AnimatePresence initial={false}>
        {statusChips.map(({ kind, count, title }) => {
          const palette = STATUS_PALETTE[kind];
          const ChipIcon = STATUS_ICON[kind];
          if (kind === "background") {
            // A detached call reads as a label, not a glyph, so the state is unambiguous at a glance.
            return (
              <motion.div
                key={kind}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.12, ease: "easeOut" }}
                style={{ display: "inline-flex", alignItems: "center" }}
              >
                <Pill colorPalette={palette} title={title}>
                  {count > 1
                    ? translation("runningDetachedCount", { count })
                    : translation("runningDetached")}
                </Pill>
              </motion.div>
            );
          }
          return (
            <motion.div
              key={kind}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <TallyBadge
                title={title}
                count={count}
                colorPalette={palette}
                icon={ChipIcon ? <ChipIcon /> : null}
              />
            </motion.div>
          );
        })}
      </AnimatePresence>
      <AnimatePresence initial={false}>
        {soleTool && toolStatus(soleTool.status) === "failed" ? (
          <motion.div
            key="failed"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            style={{ display: "inline-flex", alignItems: "center" }}
          >
            <Pill colorPalette="red">{translation("failed")}</Pill>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </>
  );

  return (
    <Box alignSelf="flex-start" w="100%" minW={0}>
      <DisclosureRow
        open={bodyOpen}
        onOpenChange={
          interactive
            ? () => setManualOverride((current) => (current === null ? true : !current))
            : undefined
        }
        tone={active ? "active" : "muted"}
        maxH={80}
        followTailKey={tools.length}
        icon={
          <Box color={headingIconColor} display="flex" alignItems="center">
            {showActivitySpinner ? <ActivitySpinner /> : <HeadingIcon />}
          </Box>
        }
        title={titleSlot}
        // `undefined` rather than an empty fragment, which is truthy and would render an empty badge row.
        badges={hasBadges ? badgeSlot : undefined}
      >
        {!interactive ? undefined : soleTool ? (
          <ToolCallDetail
            name={soleTool.name}
            arguments={soleTool.arguments}
            result={soleTool.result}
            toolCallId={soleTool.toolCallId}
            status={soleTool.status}
          />
        ) : (
          <Flex direction="column" gap={1}>
            {tools.map((tool, index) => (
              <ToolCall
                key={tool.toolCallId || `tool-${index}`}
                name={tool.name}
                arguments={tool.arguments}
                argumentsComplete={tool.argumentsComplete}
                result={tool.result}
                toolCallId={tool.toolCallId}
                status={tool.status}
                permission={tool.permission}
                question={tool.question}
              />
            ))}
          </Flex>
        )}
      </DisclosureRow>
    </Box>
  );
});
