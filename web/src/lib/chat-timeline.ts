import type { ChatMessage } from "@/lib/use-chat";

export type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool_group"; id: string; messages: ChatMessage[]; thinkingTurns: number };

export function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  let pendingThinkingId: string | null = null;
  let pendingThinkingTurns = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role === "thinking") {
      pendingThinkingId ??= message.id;
      pendingThinkingTurns += 1;
      index += 1;
      continue;
    }
    if (message.role === "assistant" && !message.content.trim()) {
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      if (pendingThinkingId) {
        items.push({
          kind: "tool_group",
          id: pendingThinkingId,
          messages: [],
          thinkingTurns: pendingThinkingTurns,
        });
      }
      items.push({ kind: "message", message });
      pendingThinkingId = null;
      pendingThinkingTurns = 0;
      index += 1;
      continue;
    }
    const toolMessages: ChatMessage[] = [];
    const groupKey = pendingThinkingId;
    let thinkingTurns = pendingThinkingTurns;
    pendingThinkingId = null;
    pendingThinkingTurns = 0;
    while (index < messages.length) {
      const next = messages[index];
      if (next.role === "tool_call") {
        toolMessages.push(next);
        index += 1;
      } else if (next.role === "thinking") {
        thinkingTurns += 1;
        index += 1;
      } else {
        break;
      }
    }
    items.push({
      kind: "tool_group",
      id: groupKey ?? toolMessages[0].id,
      messages: toolMessages,
      thinkingTurns,
    });
  }
  if (pendingThinkingId) {
    items.push({
      kind: "tool_group",
      id: pendingThinkingId,
      messages: [],
      thinkingTurns: pendingThinkingTurns,
    });
  }
  return items;
}
