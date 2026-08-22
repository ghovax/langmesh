import type { ChatMessage } from "@/lib/use-chat";
import { toolCallReady } from "@/lib/tool-event";

export type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool_group"; id: string; messages: ChatMessage[] };

function isReadyToolCall(message: ChatMessage): boolean {
  return toolCallReady({
    name: message.content,
    arguments: message.meta?.arguments as Record<string, unknown> | undefined,
    argumentsComplete: message.meta?.argumentsComplete,
  });
}

/** Whether this turn already has a row the person can see (assistant text or a ready tool). */
export function turnHasVisibleOutput(messages: ChatMessage[]): boolean {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user" || message.role === "peer" || message.role === "goal") {
      return false;
    }
    if (message.role === "assistant" && message.content.trim()) return true;
    if (message.role === "tool_call" && isReadyToolCall(message)) return true;
  }
  return false;
}

export function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    // Reasoning is live status, not a transcript row.
    if (message.role === "thinking" || (message.role === "assistant" && !message.content.trim())) {
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      items.push({ kind: "message", message });
      index += 1;
      continue;
    }
    const toolMessages: ChatMessage[] = [];
    while (index < messages.length) {
      const next = messages[index];
      if (next.role === "tool_call") {
        if (isReadyToolCall(next)) toolMessages.push(next);
        index += 1;
      } else if (next.role === "thinking") {
        index += 1;
      } else {
        break;
      }
    }
    if (toolMessages.length > 0) {
      items.push({
        kind: "tool_group",
        id: toolMessages[0].id,
        messages: toolMessages,
      });
    }
  }
  return items;
}
