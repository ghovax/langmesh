"use client";

/** This machine and the ones it can reach, so switching daemons stays on this page. */

import { Box, Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";

import { richTags } from "@/lib/i18n/rich-tags";
import { useCallback, useEffect, useState } from "react";
import { LuTrash2 } from "react-icons/lu";

import { toaster } from "@/components/ui/Toaster";
import {
  LOCAL_DAEMON_ID,
  activeDaemonId,
  addMachine,
  fetchDaemonTargets,
  forgetMachine,
  probeDaemon,
  subscribeEvents,
  switchDaemon,
  type DaemonTarget,
} from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { reportError } from "@/lib/faults";

export function MachinesPanel({
  onSelect,
}: {
  onSelect?: (target: DaemonTarget) => void;
}) {
  const translation = useTranslations("ConnectionSettings");
  const [targets, setTargets] = useState<DaemonTarget[]>([]);
  const [reachable, setReachable] = useState<Record<string, boolean>>({});
  const [activeId, setActiveId] = useState(activeDaemonId);
  const [link, setLink] = useState("");
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(() => {
    fetchDaemonTargets()
      .then(async (next) => {
        setTargets(next);
        setActiveId(activeDaemonId());
        const probes = await Promise.all(
          next.map(async (target) => [target.id, await probeDaemon(target)] as const),
        );
        setReachable(Object.fromEntries(probes));
      })
      .catch((caught) =>
        reportError({ component: "machines-panel", operation: "list the machines" }, caught),
      );
  }, []);

  useEffect(() => {
    refresh();
    return subscribeEvents((event) => {
      if (event.type === "machines_changed") refresh();
    });
  }, [refresh]);

  async function save() {
    const trimmed = link.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    try {
      await addMachine(trimmed);
      setLink("");
      refresh();
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("couldNotConnect"),
        description: errorMessage(caught),
        closable: true,
      });
    } finally {
      setSaving(false);
    }
  }

  function select(target: DaemonTarget) {
    switchDaemon(target);
    setActiveId(target.id);
    onSelect?.(target);
  }

  const remotes = targets.filter((target) => target.kind === "remote");
  const local = targets.find((target) => target.kind === "local");

  return (
    <Flex direction="column" gap={4} w="100%">
      <Flex direction="column" gap={2}>
        <Text textStyle="sectionLabel" color="fg.muted">
          {translation("savedConnections")}
        </Text>
        <Flex direction="column" gap={2}>
          {local ? (
            <MachineRow
              target={{ ...local, name: translation("thisMachine") }}
              active={activeId === LOCAL_DAEMON_ID}
              online={reachable[LOCAL_DAEMON_ID] !== false}
              onSelect={select}
            />
          ) : null}
          {remotes.length === 0 && !local ? (
            <Box borderWidth="1px" borderColor="border" borderRadius="md" px={3} py={4}>
              <Text fontSize="sm" color="fg.muted">
                {translation("noSavedConnections")}
              </Text>
              <Text fontSize="xs" color="fg.subtle" mt={1}>
                {translation("noSavedConnectionsHint")}
              </Text>
            </Box>
          ) : null}
          {remotes.map((machine) => (
            <MachineRow
              key={machine.id}
              target={machine}
              active={activeId === machine.id}
              online={reachable[machine.id] === true}
              onSelect={select}
              onForget={() => {
                void forgetMachine(machine.id)
                  .then(() => {
                    if (activeDaemonId() === machine.id) {
                      switchDaemon("local");
                      setActiveId(LOCAL_DAEMON_ID);
                      onSelect?.({
                        id: LOCAL_DAEMON_ID,
                        name: translation("thisMachine"),
                        kind: "local",
                        endpoint: local?.endpoint ?? "",
                        token: local?.token ?? "",
                      });
                    }
                    refresh();
                  })
                  .catch((caught) =>
                    reportError(
                      { component: "machines-panel", operation: "forget a machine" },
                      caught,
                    ),
                  );
              }}
            />
          ))}
        </Flex>
      </Flex>

      <Flex direction="column" gap={2}>
        <Text textStyle="sectionLabel" color="fg.muted">
          {translation("pairingLink")}
        </Text>
        <Flex gap={2} align="center">
          <Input
            size="xs"
            flex={1}
            value={link}
            placeholder={translation("pairingLinkPlaceholder")}
            onChange={(event) => setLink(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void save();
            }}
          />
          <Button
            size="xs"
            variant="solid"
            colorPalette="blue"
            loading={saving}
            disabled={!link.trim()}
            onClick={() => void save()}
          >
            {translation("saveConnection")}
          </Button>
        </Flex>
        <Text fontSize="xs" color="fg.subtle">
          {translation.rich("pairingLinkHelper", richTags)}
        </Text>
      </Flex>
    </Flex>
  );
}

function MachineRow({
  target,
  active,
  online,
  onSelect,
  onForget,
}: {
  target: DaemonTarget;
  active: boolean;
  online: boolean;
  onSelect: (target: DaemonTarget) => void;
  onForget?: () => void;
}) {
  const translation = useTranslations("ConnectionSettings");
  const host = target.endpoint.replace(/^https?:\/\//, "") || translation("thisMachine");
  return (
    <Flex
      align="center"
      gap={3}
      px={3}
      py={2}
      borderWidth="1px"
      borderColor={active ? "blue.muted" : "border"}
      borderRadius="md"
      bg="bg.panel"
    >
      <Box flex={1} minW={0}>
        <Text fontSize="sm" fontWeight="medium" truncate>
          {target.name || translation("thisMachine")}
        </Text>
        <Text fontSize="xs" color="fg.subtle" truncate>
          {host}
        </Text>
      </Box>
      {active ? (
        <Flex align="center" gap={1.5} color="green.fg" flexShrink={0}>
          <Box boxSize="1.5" borderRadius="full" bg="green.solid" />
          <Text fontSize="xs">{translation("using")}</Text>
        </Flex>
      ) : online ? (
        <Button size="xs" variant="ghost" onClick={() => onSelect(target)}>
          {translation("switchTo")}
        </Button>
      ) : (
        <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
          {translation("unreachable")}
        </Text>
      )}
      {onForget ? (
        <IconButton
          size="xs"
          variant="ghost"
          colorPalette="red"
          disabled={active}
          aria-label={translation("deleteConnection", { url: target.endpoint })}
          onClick={onForget}
        >
          <LuTrash2 />
        </IconButton>
      ) : null}
    </Flex>
  );
}
