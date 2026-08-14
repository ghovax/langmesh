"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTurn,
  abortToolCall,
  retrySessionTurn,
  attachSession,
  compactSession,
  messageParts,
  resolvePermission,
  resolveQuestion,
  fetchSessionTurns,
  sessionCreate,
  sessionSend,
  CONTENT_BLOCK_METADATA_KEY,
  METADATA_KEY,
  partPayload,
  turnState,
  type A2AMessage,
  type A2APart,
  type A2ATurn as A2ATurnWire,
  type PermissionMode,
  type SessionStreamFrame,
  type SendOutcome,
  type WorktreeStrategy,
} from "./api";
import {
  isSameToolEvent,
  type QuestionAnswer,
  type QuestionItem,
  type ToolEvent,
  type ToolEventStatus,
  type ToolPermission,
  type ToolQuestion,
} from "./tool-event";
import { toaster } from "@/components/ui/toaster";
import { swallowed } from "@/lib/swallowed";
import { useTranslations } from "next-intl";
import { asRecord } from "@/lib/coerce";
import type { PrefixDivergence, WireEvent } from "@shared/generated/events";
import { clientIdentifier } from "@/lib/identifier";
import { Outbox, type Delivery, type OutboxHold, type OutboxMessage } from "@/lib/outbox";

// Re-export the A2A task shape so components can consume it from one place.
export type A2ATurn = A2ATurnWire;

export type TaskState =
  | "submitted"
  | "working"
  | "input-required"
  | "completed"
  | "canceled"
  | "failed"
  | "rejected"
  | "auth-required"
  | "unknown";

const TERMINAL_STATES: ReadonlySet<TaskState> = new Set([
  "completed",
  "canceled",
  "failed",
  "rejected",
]);

// A file attached to a turn, as the metadata a chip renders from. Carried on the user message's meta.
export interface MessageAttachment {
  filename: string;
  path: string;
  mimeType: string;
  size: number;
}

export type TranslationParameters = Record<string, string | number>;

// The per-message side data reducers attach and views read. Every field optional and role-specific.
export interface MessageMeta {
  arguments?: Record<string, unknown>;
  argumentsComplete?: boolean;
  toolCallId?: string;
  // Spans tool lifecycle and the compaction/thinking indicators, so it is the wider string set.
  status?: string;
  result?: unknown;
  permission?: ToolPermission;
  question?: ToolQuestion;
  error?: FriendlyError;
  warning?: { code: "image_metadata_only"; parameters?: TranslationParameters };
  reason?: string;
  messagesBefore?: number;
  messagesAfter?: number;
  compactionErrorCode?:
    | "compaction_failed"
    | "compaction_no_reclaim"
    | "compaction_preparation_failed"
    | "compaction_strategy_failed";
  retrying?: boolean;
  retryFailed?: boolean;
  durationMs?: number;
  attachments?: MessageAttachment[];
  // On a peer message: which session sent it, since a report comes from somewhere with an id.
  peerSender?: string;
  goalReviewId?: string;
}

export interface ChatMessage {
  id: string;
  role:
    | "user"
    | "peer"
    // The goal review's instruction, addressed to the session but written by neither it nor the person.
    | "goal"
    | "assistant"
    | "tool_call"
    | "thinking"
    | "error"
    | "warning"
    | "compaction";
  content: string;
  timestamp: string;
  meta?: MessageMeta;
  contentBlocks?: Array<{ identifier: string; content: string }>;
}

export interface ChatTask {
  identifier: string;
  description: string;
  status: string;
  dependencies: string[];
}

// Running token totals, summed from the per-call usage the model reports.
export interface TokenUsage {
  // Cumulative session totals — the running spend, shown only in the tooltip.
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  // What a cache could have returned this session — the denominator cacheReadTokens means something against.
  cacheReachableTokens: number;
  reasoningTokens: number;
  modelCalls: number;
  // Current occupancy: the latest call's prompt plus its reply, not the cumulative sum.
  contextTokens: number; // contextInputTokens + contextOutputTokens
  contextInputTokens: number;
  contextOutputTokens: number;
  contextWindow: number;
  contextWindowEstimated: boolean;
  // What the latest call's cache did and why, since a running total cannot say which call missed.
  contextCacheReadTokens: number;
  reachableTokens: number;
  prefixIntact: boolean;
  divergence: PrefixDivergence | null;
}

// A turn's input: prose plus structured payloads, which travel as DataParts so the agent gets JSON.
export type ChatInput = { kind: "text"; text: string; dataParts?: Record<string, unknown>[] };

// What the composer still holds: messages the session has not taken, and nothing else.
export type QueuedMessage = OutboxMessage;

// The wire's ToolStatus as a UI lifecycle. `input_required` is UI-only, driven by events rather than results.
function statusFromWire(wireStatus: unknown): ToolEventStatus {
  switch (wireStatus) {
    case "ok":
      return "completed";
    case "error":
      return "failed";
    case "running":
      return "running";
    default:
      throw new Error(`Invalid tool result status: ${String(wireStatus)}`);
  }
}

function requiredContentBlockIdentifier(metadata: Record<string, unknown> | undefined): string {
  const extension = asRecord(metadata?.[CONTENT_BLOCK_METADATA_KEY]);
  const identifier = String(extension.id ?? "");
  if (!identifier) throw new Error("Assistant text is missing its content-block identity.");
  return identifier;
}

type FriendlyErrorCode =
  | "authentication_failed"
  | "connection_failed"
  | "context_window_exceeded"
  | "image_unsupported"
  | "provider_unavailable"
  | "rate_limited"
  | "request_rejected"
  | "server_error"
  | "turn_failed"
  | "turn_interrupted";

interface FriendlyError {
  code: FriendlyErrorCode;
  parameters?: TranslationParameters;
  status?: number;
}

function translationParameters(value: unknown): TranslationParameters | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const entries = Object.entries(value).filter(
    (entry): entry is [string, string | number] =>
      typeof entry[1] === "string" || typeof entry[1] === "number",
  );
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

function structuredErrorFromData(data: Record<string, unknown>): FriendlyError {
  const code = data.code;
  if (
    code !== "authentication_failed" &&
    code !== "connection_failed" &&
    code !== "context_window_exceeded" &&
    code !== "image_unsupported" &&
    code !== "provider_unavailable" &&
    code !== "rate_limited" &&
    code !== "request_rejected" &&
    code !== "server_error" &&
    code !== "turn_failed" &&
    code !== "turn_interrupted"
  ) {
    throw new Error(`Invalid turn error code: ${String(code)}`);
  }
  const parameters = translationParameters(data.parameters);
  return {
    code,
    ...(typeof data.status === "number" ? { status: data.status } : {}),
    ...(parameters ? { parameters } : {}),
  };
}

// A turn-level failure worth a toast. A tool-scoped error renders on its own card instead.
function structuredErrorFromPart(part: A2APart | undefined): FriendlyError | null {
  if (!part || part.kind !== "data") return null;
  const payload = partPayload(part.data);
  // A `tool_call_id` marks a failure belonging to one card, which renders in place.
  if (payload.kind !== "error" || payload.tool_call_id) return null;
  return structuredErrorFromData(payload);
}

/** Take a row back out of the transcript, for a message that turned out never to have been delivered. */
function dropMessage(state: ReduceState, id: string): void {
  state.messages = state.messages.filter((message) => message.id !== id);
}

function settleCompactionMarker(state: ReduceState, fallbackId: string, meta: MessageMeta): void {
  const runningIndex = state.messages.findLastIndex(
    (message) => message.role === "compaction" && message.meta?.status === "running",
  );
  if (runningIndex >= 0) {
    state.messages = state.messages.map((message, index) =>
      index === runningIndex ? { ...message, meta: { ...message.meta, ...meta } } : message,
    );
    return;
  }
  // The persisted row may already have settled this same pass under a different id; do not
  // draw a second separator beside the one the transcript owns.
  if (meta.status === "done" || meta.status === "failed") {
    const alreadySettled = state.messages.some(
      (message) => message.role === "compaction" && message.meta?.status === meta.status,
    );
    if (alreadySettled) return;
  }
  const existing = state.messages.find((message) => message.id === fallbackId);
  if (existing) {
    upsertMessage(state, { ...existing, meta: { ...existing.meta, ...meta } });
    return;
  }
  state.appendMessage({
    id: fallbackId,
    role: "compaction",
    content: "",
    timestamp: new Date().toISOString(),
    meta,
  });
}

function dropCompactionMarker(state: ReduceState, fallbackId: string): void {
  const runningIndex = state.messages.findLastIndex(
    (message) => message.role === "compaction" && message.meta?.status === "running",
  );
  if (runningIndex >= 0) {
    state.messages = state.messages.filter((_, index) => index !== runningIndex);
    return;
  }
  dropMessage(state, fallbackId);
}

function pushErrorMessage(state: ReduceState, error: FriendlyError, sourceId?: string): void {
  state.appendMessage({
    id: stableMessageId(state, "error", sourceId),
    role: "error",
    content: "",
    timestamp: new Date().toISOString(),
    meta: { error },
  });
}

function streamedMcpResult(data: Record<string, unknown>): Record<string, unknown> {
  const event = asRecord(data.event);
  return {
    server: data.server ?? event.server,
    tool: data.tool ?? event.tool,
    event: event.event,
    payload: event.payload,
    progress: event.progress,
    total: event.total,
    message: event.message,
  };
}

// Each notification is appended to `events`, while the latest values stay at the top level.
function mergeMcpResult(
  existing: unknown,
  streamed: Record<string, unknown>,
): Record<string, unknown> {
  const current = asRecord(existing);
  const events = Array.isArray(current.events) ? current.events : [];
  return {
    ...current,
    ...streamed,
    events: [...events, streamed],
  };
}

// The return value replaces the streamed state but keeps the notification log only the stream carried.
function mergeMcpFinalResult(existing: unknown, finalResult: unknown): unknown {
  const current = asRecord(existing);
  const finalRecord = asRecord(finalResult);
  if (Object.keys(finalRecord).length === 0) return finalResult;
  if (!Array.isArray(current.events)) return finalResult;
  return { ...finalRecord, events: current.events };
}

// Turning A2A stream parts into chat state. Shared by attach and replay, so both render identically.

type BufferedDelta = {
  key: string;
  channel: "text" | "thinking";
  blockIdentifier: string;
  chunks: string[];
};

class TranscriptIndex {
  private runningThinking = -1;
  private readonly blockOwners = new Map<string, number>();

  private observe(message: ChatMessage, messageIndex: number): void {
    if (isRunningThinkingMessage(message)) this.runningThinking = messageIndex;
    for (const block of message.contentBlocks ?? []) {
      this.blockOwners.set(block.identifier, messageIndex);
    }
  }

  update(previous: ChatMessage[], messages: ChatMessage[]): void {
    // Replacing rows cannot move an owner. Appending observes only the suffix; filters and prepends
    // take the one safe rebuild path. Thus streaming rows stay O(1), while structural edits converge.
    if (messages.length === previous.length) return;
    if (
      messages.length > previous.length &&
      previous.every((message, messageIndex) => message === messages[messageIndex])
    ) {
      for (let messageIndex = previous.length; messageIndex < messages.length; messageIndex += 1) {
        this.observe(messages[messageIndex], messageIndex);
      }
      return;
    }
    this.runningThinking = -1;
    this.blockOwners.clear();
    messages.forEach((message, messageIndex) => this.observe(message, messageIndex));
  }

  thinking(): number {
    return this.runningThinking;
  }

  finishThinking(): void {
    this.runningThinking = -1;
  }

  blockOwner(identifier: string): number | undefined {
    return this.blockOwners.get(identifier);
  }

  observeAppended(message: ChatMessage, messageIndex: number): void {
    this.observe(message, messageIndex);
  }
}

class LiveDeltaBuffer {
  private turnId = "";
  private readonly cursors = new Map<string, number>();
  private deltas: BufferedDelta[] = [];

  append(frame: Extract<SessionStreamFrame, { kind: "delta" }>): void {
    if (frame.turn_id !== this.turnId) {
      this.turnId = frame.turn_id;
      this.cursors.clear();
    }
    const key = `${frame.turn_id}:${frame.channel}:${frame.block_id}`;
    const expectedCursor = this.cursors.get(key) ?? 0;
    if (frame.cursor > expectedCursor) throw new Error("Live transcript stream has a gap.");
    const consumed = Math.max(0, expectedCursor - frame.cursor);
    if (consumed >= frame.chunks.length) return;
    const chunks = frame.chunks.slice(consumed);
    this.cursors.set(key, frame.cursor + frame.chunks.length);
    const previous = this.deltas.at(-1);
    if (previous?.key === key) previous.chunks.push(...chunks);
    else
      this.deltas.push({
        key,
        channel: frame.channel,
        blockIdentifier: frame.block_id,
        chunks,
      });
  }

  take(): BufferedDelta[] {
    const buffered = this.deltas;
    this.deltas = [];
    return buffered;
  }
}

class ReduceState {
  private transcript: ChatMessage[] = [];
  tasks: ChatTask[] = [];
  tokenUsage: TokenUsage | null = null; // latest cumulative token totals, if any reported
  // Per-source occurrence counter, so a key comes from the server messageId rather than array position.
  keyCounts = new Map<string, number>();
  live = new LiveDeltaBuffer();
  // Namespaces generated fallback ids while complete turns arrive independently newest-to-oldest.
  keyNamespace = "";
  // One owner for every derived location; it is rebuilt after structural edits and history merges.
  readonly index = new TranscriptIndex();

  get messages(): ChatMessage[] {
    return this.transcript;
  }

  set messages(messages: ChatMessage[]) {
    const previous = this.transcript;
    this.transcript = messages;
    this.index.update(previous, messages);
  }

  appendMessage(message: ChatMessage): void {
    this.transcript.push(message);
    this.index.observeAppended(message, this.transcript.length - 1);
  }
}

export type TranscriptState = ReduceState;

export function createTranscriptState(): TranscriptState {
  return newReduceState();
}

function newReduceState(): ReduceState {
  return new ReduceState();
}

// A position-independent id: the A2A messageId plus an occurrence counter, so replay converges.
function toolCallMessageId(toolCallId: string | undefined): string {
  return `toolcall-${toolCallId || clientIdentifier()}`;
}

function stableMessageId(state: ReduceState, prefix: string, sourceId: string | undefined): string {
  if (!sourceId) {
    // A monotonic counter, not the array length, or inserting a row earlier renumbers every row after it.
    const issued = state.keyCounts.get("") ?? 0;
    state.keyCounts.set("", issued + 1);
    return `${prefix}-${state.keyNamespace || "live"}-anon-${issued}`;
  }
  // A message that has an id is that id, so the optimistic copy and the echo are one row.
  return `${prefix}-${sourceId}`;
}

function upsertMessage(state: ReduceState, message: ChatMessage): void {
  // Replace in place when the row exists, append when it does not, so a message arriving twice converges.
  const index = state.messages.findIndex((existing) => existing.id === message.id);
  if (index === -1) {
    state.appendMessage(message);
    return;
  }
  state.messages = state.messages.map((existing, position) =>
    position === index
      ? { ...existing, ...message, meta: { ...existing.meta, ...message.meta } }
      : existing,
  );
}

function asChatTask(value: unknown): ChatTask | null {
  const record = asRecord(value);
  const identifier = String(record.identifier ?? "");
  const description = String(record.description ?? "");
  if (!identifier || !description) return null;
  return {
    identifier,
    description,
    status: String(record.status ?? "pending"),
    dependencies: Array.isArray(record.dependencies) ? record.dependencies.map(String) : [],
  };
}

function mergeTasks(current: ChatTask[], updates: unknown[]): ChatTask[] {
  const next = [...current];
  for (const raw of updates) {
    const task = asChatTask(raw);
    if (!task) continue;
    const index = next.findIndex((item) => item.identifier === task.identifier);
    if (index === -1) next.push(task);
    else next[index] = { ...next[index], ...task };
  }
  return next;
}

function isRunningThinkingMessage(message: ChatMessage): boolean {
  return message.role === "thinking" && message.meta?.status === "running";
}

function finishRunningThinking(state: ReduceState): void {
  const index = state.index.thinking();
  if (index < 0) return;
  const message = state.messages[index];
  if (message && isRunningThinkingMessage(message)) {
    state.messages[index] = { ...message, meta: { ...message.meta, status: "done" } };
  }
  state.index.finishThinking();
}

// Close the in-flight thinking row with its measured duration, so it reads "Thought for Ns".
function finishRunningThinkingWithDuration(state: ReduceState, durationMs: number): void {
  const index = state.index.thinking();
  if (index < 0) return;
  const message = state.messages[index];
  if (message && isRunningThinkingMessage(message)) {
    state.messages[index] = {
      ...message,
      meta: { ...message.meta, status: "done", durationMs },
    };
  }
  state.index.finishThinking();
}

function finishActiveTools(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && message.meta?.status === "running"
      ? { ...message, meta: { ...message.meta, status: "completed" } }
      : message,
  );
}

// The one path for the thinking signal: ensure a running row exists, then append the reasoning.
function applyThinking(state: ReduceState, text: string): void {
  let index = state.index.thinking();
  if (index === -1) {
    // Counted in its own right, not by the array's length nor by the shared anonymous counter: both advance
    // differently on a live turn and on its replay, and any drift re-keys every reasoning row after it.
    const issued = state.keyCounts.get("thinking") ?? 0;
    state.keyCounts.set("thinking", issued + 1);
    const message: ChatMessage = {
      id: `status-${state.keyNamespace || "live"}-${issued}`,
      role: "thinking",
      content: "",
      timestamp: new Date().toISOString(),
      meta: { status: "running" },
    };
    state.appendMessage(message);
    index = state.messages.length - 1;
  }
  // The session is reasoning, so a row opened optimistically stops being provisional.
  const message = state.messages[index];
  state.messages[index] = { ...message, content: message.content + text };
}

function hasAssistantTextAfterLastUser(state: ReduceState): boolean {
  const lastUserIndex = state.messages.findLastIndex((message) => message.role === "user");
  return state.messages
    .slice(lastUserIndex + 1)
    .some((message) => message.role === "assistant" && message.content.trim());
}

function toolEventFromMessage(message: ChatMessage): ToolEvent | null {
  if (message.role !== "tool_call") return null;
  const status = message.meta?.status;
  return {
    name: message.content,
    arguments: message.meta?.arguments as Record<string, unknown> | undefined,
    argumentsComplete: message.meta?.argumentsComplete,
    toolCallId: String(message.meta?.toolCallId ?? ""),
    result: message.meta?.result,
    status:
      status === "running" ||
      status === "completed" ||
      status === "done" ||
      status === "failed" ||
      status === "input_required"
        ? status
        : undefined,
    permission: message.meta?.permission as ToolEvent["permission"],
  };
}

function messageMatchesToolEvent(message: ChatMessage, name: string, toolCallId: string): boolean {
  const event = toolEventFromMessage(message);
  return event ? isSameToolEvent(event, name, toolCallId) : false;
}

function appendAssistantContentBlock(
  message: ChatMessage,
  text: string,
  blockIdentifier: string,
): ChatMessage {
  if (!message.contentBlocks) {
    throw new Error("Assistant messages require structured content blocks.");
  }
  const existingContentBlocks = message.contentBlocks;
  // Merged by identity, not position: a later delta belongs in its block wherever that has ended up.
  const existingIndex = existingContentBlocks.findIndex(
    (contentBlock) => contentBlock.identifier === blockIdentifier,
  );
  let contentBlocks: Array<{ identifier: string; content: string }>;
  if (existingIndex >= 0) {
    existingContentBlocks[existingIndex] = {
      ...existingContentBlocks[existingIndex],
      content: existingContentBlocks[existingIndex].content + text,
    };
    contentBlocks = existingContentBlocks;
  } else {
    contentBlocks = [...existingContentBlocks, { identifier: blockIdentifier, content: text }];
  }
  return { ...message, content: message.content + text, contentBlocks };
}

function pushAssistantText(state: ReduceState, text: string, blockIdentifier: string): void {
  if (!text) return;
  if (!blockIdentifier) throw new Error("Assistant text requires a content-block identity.");
  finishRunningThinking(state);
  const ownerIndex = state.index.blockOwner(blockIdentifier);
  if (ownerIndex !== undefined) {
    state.messages[ownerIndex] = appendAssistantContentBlock(
      state.messages[ownerIndex],
      text,
      blockIdentifier,
    );
    return;
  }
  const message: ChatMessage = {
    // The block it opened with, which the live stream and a replay both carry, so a settled turn is
    // re-rendered rather than rebuilt: a new key here unmounts the prose, its highlighting and its diagrams.
    id: `asst-${blockIdentifier}`,
    role: "assistant",
    content: text,
    contentBlocks: [{ identifier: blockIdentifier, content: text }],
    timestamp: new Date().toISOString(),
  };
  state.appendMessage(message);
}

// Attachments into the lean shape the UI renders, shared by the live send and replay so chips match.
function attachmentsFromData(data: Record<string, unknown> | undefined): MessageAttachment[] {
  if (!data || data.kind !== "attachments") return [];
  const raw = Array.isArray(data.attachments) ? data.attachments : [];
  const attachments: MessageAttachment[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    const path = String(record.path ?? "");
    const filename = String(record.filename ?? record.title ?? "");
    if (!path && !filename) continue;
    attachments.push({
      filename: filename || path.split("/").pop() || "attachment",
      path,
      mimeType: String(record.mime_type ?? ""),
      size: Number(record.size ?? 0),
    });
  }
  return attachments;
}

function attachmentsFromMessage(message: A2AMessage): MessageAttachment[] {
  const attachments: MessageAttachment[] = [];
  for (const part of message.parts ?? []) {
    if (part.kind === "data") attachments.push(...attachmentsFromData(partPayload(part.data)));
  }
  return attachments;
}

// When the session took a message, as it recorded it, so a conversation reloaded later keeps its clock.
function receivedAt(message: A2AMessage): string {
  const extension = asRecord(message.metadata?.[METADATA_KEY]);
  const stamp = extension.receivedAt;
  return typeof stamp === "string" ? stamp : "";
}

// A message addressed to the session. Three voices arrive this way and the reader has to tell them apart:
// the person's own words, a peer's report, and the goal review's instruction.
function reduceInboundMessage(
  state: ReduceState,
  message: A2AMessage,
  peerSender = "",
  fromGoalReview = false,
  turnGoalReviewId = "",
): void {
  const text = (message.parts ?? [])
    .filter((part) => part.kind === "text")
    .map((part) => part.text ?? "")
    .join("");
  const attachments = attachmentsFromMessage(message);
  const messageTurnMetadata = asRecord(message.metadata?.[METADATA_KEY]);
  const goalReviewId = fromGoalReview
    ? String(messageTurnMetadata.goalReviewId ?? turnGoalReviewId).trim()
    : "";
  // No prose and no attachments is nothing to render, and replay must match the live path exactly.
  if (!text.trim() && attachments.length === 0) return;
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(peerSender ? { peerSender } : {}),
    ...(goalReviewId ? { goalReviewId } : {}),
  };
  const role = fromGoalReview ? "goal" : peerSender ? "peer" : "user";
  upsertMessage(state, {
    id: stableMessageId(state, role, message.messageId),
    role,
    content: text,
    // When the session took it, from the message itself, or a reloaded transcript dates everything to now.
    timestamp: receivedAt(message) || new Date().toISOString(),
    ...(Object.keys(meta).length > 0 ? { meta } : {}),
  });
}

// The one reduction of a part, walked by replay and by the live tail alike.
function reduceAgentPart(state: ReduceState, part: A2APart, sourceId?: string): void {
  drainLiveDeltas(state);
  if (part.kind === "text") {
    pushAssistantText(state, part.text ?? "", requiredContentBlockIdentifier(part.metadata));
    return;
  }
  if (part.kind !== "data" || !part.data) return;
  reduceDataPart(state, partPayload(part.data), sourceId);
}

function reduceLiveDelta(
  state: ReduceState,
  frame: Extract<SessionStreamFrame, { kind: "delta" }>,
): void {
  state.live.append(frame);
}

function drainLiveDeltas(state: ReduceState): void {
  for (const delta of state.live.take()) {
    const text = delta.chunks.join("");
    if (delta.channel === "thinking") applyThinking(state, text);
    else pushAssistantText(state, text, delta.blockIdentifier);
  }
}

export function appendTranscriptPart(
  state: TranscriptState,
  part: A2APart,
  sourceId?: string,
): void {
  reduceAgentPart(state, part, sourceId);
}

export function appendTranscriptDelta(
  state: TranscriptState,
  frame: Extract<SessionStreamFrame, { kind: "delta" }>,
): void {
  reduceLiveDelta(state, frame);
  drainLiveDeltas(state);
}

function reduceAgentMessage(state: ReduceState, message: A2AMessage): void {
  for (const part of message.parts ?? []) reduceAgentPart(state, part, message.messageId);
}

function reduceDataPart(
  state: ReduceState,
  data: Record<string, unknown>,
  sourceId?: string,
): void {
  // Every event here belongs to this session, and is read through the generated union's discriminant.
  const event = data as unknown as WireEvent;
  switch (event.kind) {
    case "inbound_message": {
      const text = (event.text ?? "").trim();
      if (!text) break;
      // Delivered once however often it arrives, since attaching replays turns the session already holds.
      const steeringSender = (event.peer_sender ?? "").trim();
      const goalReviewId = (event.goal_review_id ?? "").trim();
      const role = goalReviewId ? "goal" : steeringSender ? "peer" : "user";
      const identifier = (event.message_id ?? "").trim();
      const alreadyShown = identifier
        ? state.messages.some((message) => message.id === `${role}-${identifier}`)
        : state.messages.some(
            (message) =>
              message.role === role &&
              message.content === text &&
              !!sourceId &&
              message.id.startsWith(`${role}-${sourceId}-`),
          );
      if (alreadyShown) break;
      if (role === "user") {
        // Sending new work explicitly supersedes any still-retryable failed turn.
        state.messages = state.messages.filter((message) => message.role !== "error");
      }
      state.appendMessage({
        id: identifier ? `${role}-${identifier}` : stableMessageId(state, role, sourceId),
        role,
        content: text,
        timestamp: new Date().toISOString(),
        ...(steeringSender || goalReviewId
          ? {
              meta: {
                ...(steeringSender ? { peerSender: steeringSender } : {}),
                ...(goalReviewId ? { goalReviewId } : {}),
              },
            }
          : {}),
      });
      break;
    }
    case "compaction": {
      // A compaction marker: a live indicator, then a separator, rendered as a divider rather than a bubble.
      if (event.status === "started") {
        // Sends are blocked after a failure, so the next fold start is its explicit retry. Reuse
        // that marker on both the live path and history replay; otherwise the stale failure would
        // survive beside the successful retry and keep the composer disabled forever.
        const activeIndex = state.messages.findLastIndex(
          (message) =>
            message.role === "compaction" &&
            (message.meta?.status === "running" || message.meta?.status === "failed"),
        );
        if (activeIndex >= 0) {
          state.messages = state.messages.map((message, index) =>
            index === activeIndex
              ? {
                  ...message,
                  meta: { status: "running", reason: event.reason ?? "" },
                }
              : message,
          );
        } else {
          state.appendMessage({
            id: stableMessageId(state, "compaction", sourceId),
            role: "compaction",
            content: "",
            timestamp: new Date().toISOString(),
            meta: { status: "running", reason: event.reason ?? "" },
          });
        }
        break;
      }
      if (event.status === "done") {
        const failed = event.ok === false;
        const changed =
          event.ok !== false && (event.messages_after ?? 0) < (event.messages_before ?? 0);
        const runningIndex = state.messages.findLastIndex(
          (message) => message.role === "compaction" && message.meta?.status === "running",
        );
        if (!changed && !failed) {
          // Nothing was compacted — remove the running indicator so no separator lingers.
          if (runningIndex >= 0)
            state.messages = state.messages.filter((_, index) => index !== runningIndex);
          break;
        }
        const meta = {
          status: failed ? "failed" : "done",
          reason: event.reason ?? "",
          messagesBefore: event.messages_before ?? 0,
          messagesAfter: event.messages_after ?? 0,
          ...(failed && event.error_code ? { compactionErrorCode: event.error_code } : {}),
        };
        if (runningIndex >= 0) {
          state.messages = state.messages.map((message, index) =>
            index === runningIndex ? { ...message, meta } : message,
          );
        } else {
          state.appendMessage({
            id: stableMessageId(state, "compaction", sourceId),
            role: "compaction",
            content: "",
            timestamp: new Date().toISOString(),
            meta,
          });
        }
      }
      break;
    }
    case "retry": {
      const errorIndex = state.messages.findLastIndex((message) => message.role === "error");
      if (errorIndex < 0) break;
      if (event.status === "started") {
        state.messages = state.messages.map((message, index) =>
          index === errorIndex
            ? { ...message, meta: { ...message.meta, retrying: true } }
            : message,
        );
      } else if (event.ok !== false) {
        state.messages = state.messages.filter((_, index) => index !== errorIndex);
      } else {
        state.messages = state.messages.map((message, index) =>
          index === errorIndex
            ? { ...message, meta: { ...message.meta, retrying: false, retryFailed: true } }
            : message,
        );
      }
      break;
    }
    case "token_usage": {
      // The cumulative totals grow monotonically, so the latest part is authoritative on either path.
      const cumulative = event.cumulative;
      // Per-call (latest) figures — this is the actual current context, not a sum.
      const contextInputTokens = event.input_tokens ?? 0;
      const contextOutputTokens = event.output_tokens ?? 0;
      state.tokenUsage = {
        inputTokens: cumulative?.input_tokens ?? 0,
        outputTokens: cumulative?.output_tokens ?? 0,
        totalTokens: cumulative?.total_tokens ?? 0,
        cacheReadTokens: cumulative?.cache_read_tokens ?? 0,
        cacheReachableTokens: cumulative?.reachable_tokens ?? 0,
        reasoningTokens: cumulative?.reasoning_tokens ?? 0,
        modelCalls: cumulative?.model_calls ?? 0,
        contextInputTokens,
        contextOutputTokens,
        contextTokens: contextInputTokens + contextOutputTokens,
        contextWindow: event.context_window ?? 0,
        contextWindowEstimated: event.context_window_estimated ?? false,
        contextCacheReadTokens: event.cache_read_tokens ?? 0,
        reachableTokens: event.reachable_tokens ?? 0,
        prefixIntact: event.prefix_intact ?? false,
        divergence: event.divergence ?? null,
      };
      break;
    }
    case "status": {
      // Paused on tool execution: tools surface their own status, so just close the thinking indicator.
      if (event.code === "waiting_for_tools") finishRunningThinking(state);
      // The request is with the provider, so the wait is said out loud rather than looking idle.
      if (event.code === "awaiting_model") applyThinking(state, "");
      // Anything else a status says is shown by its own row; this arm stops it reaching the unknown path.
      break;
    }
    case "thinking":
      // A new reasoning phase ends the current prose block, or text after it merges into the prior message.
      applyThinking(state, event.text ?? "");
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, event.duration_milliseconds ?? 0);
      break;
    case "tool_call": {
      // Text either side of a tool call is separate prose, and the model gives each block its own identity.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      // One row per tool call: a call stopped for approval is announced twice, and both are the same call.
      const existing = state.messages.findIndex(
        (message) =>
          message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId,
      );
      if (existing >= 0 && toolCallId) {
        state.messages = state.messages.map((message, index) =>
          index === existing
            ? {
                ...message,
                content: event.tool_name || message.content,
                // The second announcement is the call running for real: keep what the prompt attached to it.
                meta: {
                  ...message.meta,
                  arguments: event.arguments ?? message.meta?.arguments,
                  argumentsComplete: event.arguments_complete === true,
                  status: "running",
                },
              }
            : message,
        );
        break;
      }
      state.appendMessage({
        id: toolCallMessageId(toolCallId),
        role: "tool_call",
        content: event.tool_name || "unknown",
        timestamp: new Date().toISOString(),
        meta: {
          arguments: event.arguments,
          argumentsComplete: event.arguments_complete === true,
          toolCallId,
          status: "running",
        },
      });
      break;
    }
    case "tool_result": {
      finishRunningThinking(state);
      const toolName = event.tool_name;
      const toolCallId = event.tool_call_id;
      const currentMessage = state.messages.find((message) =>
        messageMatchesToolEvent(message, toolName, toolCallId),
      );
      const mergedResult =
        toolName === "call_mcp_server_tool"
          ? mergeMcpFinalResult(currentMessage?.meta?.result, event.display)
          : event.display;
      const resultStatus = statusFromWire(event.status);
      // The task tools complete through this same path, carrying the authoritative list in their result.
      const resultTasks = asRecord(mergedResult).tasks;
      if (Array.isArray(resultTasks)) state.tasks = mergeTasks(state.tasks, resultTasks);
      let matched = false;
      state.messages = state.messages.map((message) =>
        messageMatchesToolEvent(message, toolName, toolCallId)
          ? ((matched = true),
            {
              ...message,
              meta: {
                ...message.meta,
                argumentsComplete: true,
                status: resultStatus,
                result: mergedResult,
              },
            })
          : message,
      );
      if (!matched) {
        state.appendMessage({
          id: toolCallMessageId(toolCallId),
          role: "tool_call",
          content: toolName || "unknown",
          timestamp: new Date().toISOString(),
          meta: {
            argumentsComplete: true,
            toolCallId,
            status: resultStatus,
            result: mergedResult,
          },
        });
      }
      break;
    }
    case "mcp_event": {
      const toolCallId = event.tool_call_id;
      const streamed = streamedMcpResult(data);
      const currentMessage = state.messages.find((message) =>
        messageMatchesToolEvent(message, "call_mcp_server_tool", toolCallId),
      );
      const mergedResult = mergeMcpResult(currentMessage?.meta?.result, streamed);
      state.messages = state.messages.map((message) =>
        messageMatchesToolEvent(message, "call_mcp_server_tool", toolCallId)
          ? { ...message, meta: { ...message.meta, status: "running", result: mergedResult } }
          : message,
      );
      break;
    }
    case "permission_request": {
      // The approval lives on the tool call that triggered it, so the command and its output stay together.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const permission = {
        requestId: event.request_id,
        explanation: event.explanation || undefined,
        // The harness's own reason, as facts, written into a sentence by whoever draws the prompt.
        reason: event.reason ?? undefined,
      };
      const attachedPermission = state.messages.some(
        (message) =>
          message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId,
      );
      if (attachedPermission) {
        state.messages = state.messages.map((message) =>
          message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
            ? {
                ...message,
                meta: {
                  ...message.meta,
                  argumentsComplete: true,
                  status: "input_required",
                  permission,
                },
              }
            : message,
        );
      } else {
        // No card yet, and that is ordinary: approval is decided in preflight, before the call is announced.
        const raised: ChatMessage = {
          id: toolCallMessageId(toolCallId),
          role: "tool_call",
          content: event.tool_name || "",
          timestamp: new Date().toISOString(),
          meta: {
            toolCallId: toolCallId ?? "",
            argumentsComplete: true,
            status: "input_required",
            permission,
            arguments: event.arguments ?? {},
          },
        };
        state.appendMessage(raised);
      }
      break;
    }
    case "question": {
      // An ask_user prompt attaches to its tool call, same lifecycle as a permission.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const question = {
        requestId: event.request_id,
        questions: (event.questions as unknown as QuestionItem[]) ?? [],
      };
      state.messages = state.messages.map((message) =>
        message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
          ? { ...message, meta: { ...message.meta, status: "input_required", question } }
          : message,
      );
      break;
    }
    case "error": {
      finishRunningThinking(state);
      const toolName = event.tool_name ?? "";
      const toolCallId = event.tool_call_id ?? "";
      if (toolCallId) {
        // Mark the call failed generically: the raw text is model-facing and must not leak to the UI.
        let matched = false;
        state.messages = state.messages.map((message) =>
          messageMatchesToolEvent(message, toolName, toolCallId)
            ? ((matched = true),
              {
                ...message,
                meta: { ...message.meta, status: "failed", result: { code: "tool_error" } },
              })
            : message,
        );
        if (matched) break;
        // A tool-scoped error with no card is still model-facing, so swallow it rather than raise a toast.
        break;
      }
      const retryIndex = state.messages.findLastIndex(
        (message) => message.role === "error" && message.meta?.retryFailed === true,
      );
      if (retryIndex >= 0) {
        state.messages = state.messages.map((message, index) =>
          index === retryIndex
            ? {
                ...message,
                meta: { ...message.meta, error: structuredErrorFromData(data), retryFailed: false },
              }
            : message,
        );
      } else {
        pushErrorMessage(state, structuredErrorFromData(data), sourceId);
      }
      break;
    }
    case "warning": {
      finishRunningThinking(state);
      state.appendMessage({
        id: stableMessageId(state, "warning", sourceId),
        role: "warning",
        content: "",
        timestamp: new Date().toISOString(),
        meta: {
          warning: { code: event.code, parameters: translationParameters(event.parameters) },
        },
      });
      break;
    }
    case "text":
    case "done":
      // Streamed prose arrives as text parts and a turn's end as the stream's own frame; neither is reduced here.
      break;
    default: {
      // Exhaustiveness: a new WireEvent kind that is not handled above is a compile error.
      const _exhaustive: never = event;
      void _exhaustive;
      break;
    }
  }
}

export function replayTurns(turns: A2ATurn[], keyNamespace = ""): TranscriptState {
  const mainTurns = turns.filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length);
  const state: ReduceState = newReduceState();
  state.keyNamespace = keyNamespace;
  for (const turn of mainTurns) {
    // A turn's stream is its history plus its trailing status message, which A2A folds in only on the next update.
    const replayMessages = [...(turn.history ?? [])];
    const trailing = turn.status?.message;
    if (
      trailing &&
      !replayMessages.some(
        (message) => !!message.messageId && message.messageId === trailing.messageId,
      )
    ) {
      replayMessages.push(trailing);
    }
    // Stamped on the turn, not on the message, because it describes what opened the turn.
    const opened = turnState(turn);
    const peerSender = opened.peerSender ?? "";
    const fromGoalReview = opened.kind === "goal";
    const goalReviewId = opened.goalReviewId ?? "";
    for (const message of replayMessages) {
      if (message.role === "user")
        reduceInboundMessage(state, message, peerSender, fromGoalReview, goalReviewId);
      else reduceAgentMessage(state, message);
    }
    if (!hasAssistantTextAfterLastUser(state)) {
      for (const artifact of turn.artifacts ?? []) {
        for (const part of artifact.parts ?? []) {
          if (part.kind !== "text" || !part.text?.trim()) continue;
          pushAssistantText(state, part.text, requiredContentBlockIdentifier(part.metadata));
        }
      }
    }
    if (TERMINAL_STATES.has(turn.status?.state as TaskState)) finishActiveTools(state);
  }
  return state;
}

/** Complete newest-to-oldest turns, accumulated cheaply and prepended once at the next paint. */
export class TranscriptHistoryBuffer {
  private segments: ChatMessage[][] = [];
  private readonly tasks = new Map<string, ChatTask>();
  private newestTokenUsage: TokenUsage | null | undefined;
  private bufferedMessages = 0;
  private paintedMessages = 0;

  append(turn: A2ATurn, keyNamespace = String(turn.id ?? "history")): boolean {
    const replayed = replayTurns([turn], keyNamespace);
    this.segments.push(replayed.messages);
    this.bufferedMessages += replayed.messages.length;
    for (const task of replayed.tasks) {
      if (!this.tasks.has(task.identifier)) this.tasks.set(task.identifier, task);
    }
    if (this.newestTokenUsage === undefined) this.newestTokenUsage = replayed.tokenUsage;
    // Paint the newest turn immediately, then double the visible history each time. Consequently
    // all flattening across an arbitrarily long history copies fewer than twice its final rows.
    return this.bufferedMessages >= Math.max(1, this.paintedMessages);
  }

  drainInto(state: TranscriptState): boolean {
    if (this.segments.length === 0) return false;
    const seen = new Set(state.messages.map((message) => message.id));
    const acceptedNewestFirst: ChatMessage[][] = [];
    for (const segment of this.segments) {
      const accepted: ChatMessage[] = [];
      for (const message of segment) {
        if (seen.has(message.id)) continue;
        seen.add(message.id);
        accepted.push(message);
      }
      if (accepted.length > 0) acceptedNewestFirst.push(accepted);
    }
    const olderMessages = acceptedNewestFirst.reverse().flat();
    const transcriptWasEmpty = state.messages.length === 0;
    state.messages = [...olderMessages, ...state.messages];
    const mergedTasks = new Map(this.tasks);
    state.tasks.forEach((task) => mergedTasks.set(task.identifier, task));
    state.tasks = [...mergedTasks.values()];
    if (state.tokenUsage === null && transcriptWasEmpty) {
      state.tokenUsage = this.newestTokenUsage ?? null;
    }
    this.paintedMessages += olderMessages.length;
    this.clearBuffered();
    return true;
  }

  private clearBuffered(): void {
    this.segments = [];
    this.tasks.clear();
    this.newestTokenUsage = undefined;
    this.bufferedMessages = 0;
  }

  reset(): void {
    this.clearBuffered();
    this.paintedMessages = 0;
  }
}

export function useChat(
  agent: string,
  initialSessionId: string | null = null,
  workingDirectory?: string,
  worktreeStrategy: WorktreeStrategy = "none",
  permissionMode: PermissionMode = "ask",
  // Whether a turn is running on this session, which drives the live stream when we are watching it.
  sessionRunning: boolean = false,
  // The workspace this session belongs to, so the server resolves the locations the agent may address.
  workspaceId: string = "",
) {
  // Every message this hook puts in front of a person goes through the catalogue.
  const translation = useTranslations("ChatErrors");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<ChatTask[]>([]);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  // Set when every attempt to load the transcript failed, so the panel can offer a retry.
  const [historyError, setHistoryError] = useState(false);
  const [isHistoryStreaming, setIsHistoryStreaming] = useState(!!initialSessionId);
  // Bumped to force the history-load effect to re-run (a manual retry).
  const [historyReloadNonce, setHistoryReloadNonce] = useState(0);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [outboxHold, setOutboxHold] = useState<OutboxHold>(null);
  // Set when the session was created under a stricter mode than the one asked for.
  const [grantedPermissionMode, setGrantedPermissionMode] = useState<PermissionMode | null>(null);
  const [deliveringMessage, setDeliveringMessage] = useState<string | null>(null);

  // This hook's attach subscription while it drives a turn. Closing it only drops the client end.
  const attachRef = useRef<{ abort: () => void; ready: Promise<boolean> } | null>(null);
  const viewerAttachRef = useRef<{ abort: () => void; ready: Promise<boolean> } | null>(null);
  const viewerContextRef = useRef<string | null>(null);
  const historyReloadAppliedRef = useRef(0);
  const stateRef = useRef<ReduceState>(newReduceState());
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const isStreamingRef = useRef(false);
  // Set by a Stop so the stream close does not drain the queue into a fresh turn. One-shot.
  const abortedByUserRef = useRef(false);
  const errorToastKeysRef = useRef<Set<string>>(new Set());
  // True once we have driven a turn: the live stream is then authoritative, so never subscribe as well.
  const streamedLocallyRef = useRef(false);
  const startTurnRef = useRef<(message: OutboxMessage) => Promise<Delivery>>(async () => "failed");
  const flushFrameRef = useRef<number | null>(null);
  const historyBufferRef = useRef(new TranscriptHistoryBuffer());

  // Whether the transcript holds a decision nobody has made: a parked session has stopped, not paused.
  const hasPendingDecision = useCallback(
    () =>
      stateRef.current.messages.some(
        (message) => message.role === "tool_call" && message.meta?.status === "input_required",
      ),
    [],
  );

  // A message the session would not take. Not an error: it is queued, visible, and goes out on the decision.
  const notifyHeldForDecision = useCallback(
    (waitingOn: SendOutcome["waitingOn"]) => {
      if (!waitingOn) return;
      const described =
        waitingOn.kind === "question"
          ? translation("waitingOnQuestion")
          : waitingOn.command
            ? translation("waitingOnCommand", { command: waitingOn.command })
            : translation("waitingOnPermission");
      toaster.create({
        type: "info",
        title: translation("heldForDecisionTitle"),
        description: translation("heldForDecisionBody", {
          waitingOn: described,
        }),
        closable: true,
      });
    },
    [translation],
  );

  const flushNow = useCallback(() => {
    if (flushFrameRef.current != null) {
      window.cancelAnimationFrame(flushFrameRef.current);
      flushFrameRef.current = null;
    }
    drainLiveDeltas(stateRef.current);
    historyBufferRef.current.drainInto(stateRef.current);
    // Reducers mutate indexed rows in place between paints; one shallow snapshot per paint makes
    // React observe the batch without copying the entire transcript for every provider chunk.
    setMessages([...stateRef.current.messages]);
    setTasks(stateRef.current.tasks);
    setTokenUsage(stateRef.current.tokenUsage);
  }, []);

  const flush = useCallback(() => {
    if (typeof window === "undefined") {
      drainLiveDeltas(stateRef.current);
      setMessages([...stateRef.current.messages]);
      setTasks(stateRef.current.tasks);
      setTokenUsage(stateRef.current.tokenUsage);
      return;
    }
    if (flushFrameRef.current != null) return;
    flushFrameRef.current = window.requestAnimationFrame(() => {
      flushFrameRef.current = null;
      flushNow();
    });
  }, [flushNow]);

  const messageTranslation = useTranslations("ChatMessage");
  const notifyTurnError = useCallback(
    (part: A2APart | undefined) => {
      const error = structuredErrorFromPart(part);
      if (!error) return;
      const key = `${sessionIdRef.current || "turn"}:${error.code}:${error.status ?? ""}`;
      if (errorToastKeysRef.current.has(key)) return;
      errorToastKeysRef.current.add(key);
      toaster.create({
        type: "error",
        title: messageTranslation(`errors.${error.code}.title`),
        description: messageTranslation(`errors.${error.code}.body`, error.parameters),
        closable: true,
      });
    },
    [messageTranslation],
  );

  // Close this hook's stream on unmount, or orphaned streams exhaust the browser's connection pool.
  useEffect(() => {
    return () => {
      if (flushFrameRef.current != null) {
        window.cancelAnimationFrame(flushFrameRef.current);
        flushFrameRef.current = null;
      }
      attachRef.current?.abort();
      viewerAttachRef.current?.abort();
    };
  }, []);

  const prependHistoryTurn = useCallback(
    (turn: A2ATurn) => {
      if (historyBufferRef.current.append(turn)) flush();
    },
    [flush],
  );

  useEffect(() => {
    if (!initialSessionId) return;
    // The turn-driving attachment owns a newly created session until its terminal edge. The
    // session-list update will re-run this effect afterward and install the durable viewer.
    if (streamedLocallyRef.current) return;
    let cancelled = false;
    let attached = false;
    const resetTranscript =
      viewerContextRef.current !== initialSessionId ||
      historyReloadAppliedRef.current !== historyReloadNonce;
    viewerContextRef.current = initialSessionId;
    historyReloadAppliedRef.current = historyReloadNonce;
    if (resetTranscript) {
      stateRef.current = newReduceState();
      historyBufferRef.current.reset();
    }
    sessionIdRef.current = initialSessionId;
    setSessionId(initialSessionId);
    if (resetTranscript) flushNow();
    setHistoryError(false);
    if (resetTranscript) setIsHistoryLoading(true);
    setIsHistoryStreaming(true);
    const subscription = attachSession(
      initialSessionId,
      (frame) => {
        if (cancelled || streamedLocallyRef.current) return;
        if (frame.kind === "snapshot") {
          attached = true;
          setHistoryError(false);
          if (frame.reconnected) setIsHistoryStreaming(true);
        } else if (frame.kind === "history") {
          prependHistoryTurn(frame.turn);
          setIsHistoryLoading(false);
        } else if (frame.kind === "history_done") {
          flushNow();
          setIsHistoryLoading(false);
          setIsHistoryStreaming(false);
        } else if (frame.kind === "live") {
          reduceAgentPart(stateRef.current, frame.part);
          flush();
          setIsHistoryLoading(false);
        } else if (frame.kind === "delta") {
          reduceLiveDelta(stateRef.current, frame);
          flush();
          setIsHistoryLoading(false);
        }
      },
      () => {
        if (cancelled) return;
        setIsHistoryLoading(false);
        setIsHistoryStreaming(false);
        if (!attached) setHistoryError(true);
      },
    );
    viewerAttachRef.current = subscription;

    return () => {
      cancelled = true;
      subscription.abort();
      if (viewerAttachRef.current === subscription) viewerAttachRef.current = null;
    };
  }, [initialSessionId, historyReloadNonce, sessionRunning, flushNow, flush, prependHistoryTurn]);

  // Deliberately no drain here or at a turn's end: both read state that delivery itself changed.

  // Manual retry after a failed load, clearing the per-session guard so it actually re-fetches.
  const reloadHistory = useCallback(() => {
    setHistoryError(false);
    setIsHistoryLoading(true);
    setIsHistoryStreaming(true);
    setHistoryReloadNonce((nonce) => nonce + 1);
  }, []);

  // Start a turn with one message and answer what became of it, so a refusal is not silently drawn.
  const startTurn = useCallback(
    (input: OutboxMessage): Promise<Delivery> =>
      new Promise<Delivery>((settleDelivery) => {
        const dataParts = input.dataParts ?? [];
        const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
        const meta = attachments.length > 0 ? { attachments } : {};
        // The id the composer gave this message, carried onto the wire, so the copy and the echo are one row.
        const userMessageId = input.id;
        const withdrawOptimistic = () => {
          // Refused, so the row goes and the queued card stays the only place it is shown.
          dropMessage(stateRef.current, stableMessageId(stateRef.current, "user", userMessageId));
          flushNow();
        };
        const showOptimistically = () => {
          upsertMessage(stateRef.current, {
            id: stableMessageId(stateRef.current, "user", userMessageId),
            role: "user",
            content: input.text,
            timestamp: new Date().toISOString(),
            ...(Object.keys(meta).length > 0 ? { meta } : {}),
          });
          // No thinking row here: the session says when it is thinking, and until then it has not started.
          flushNow();
        };

        isStreamingRef.current = true;
        streamedLocallyRef.current = true;
        setIsStreaming(true);

        const text = input.text;

        // Three unordered things can end a turn, so this settles once whichever arrives first.
        let settled = false;
        const finishTurn = () => {
          if (settled) return;
          settled = true;
          // Stop watching: the stream belongs to the session and would leak one connection per turn.
          attachRef.current?.abort();
          attachRef.current = null;
          // A Stop or a dropped connection ends a turn with no terminal event, leaving cards spinning.
          drainLiveDeltas(stateRef.current);
          finishRunningThinking(stateRef.current);
          finishActiveTools(stateRef.current);
          // Commit the terminal transcript and streaming flag together, so no stale intermediate layout can paint.
          flushNow();
          // The queue is not touched here: a turn ending must not be both a trigger and a deletion.
          abortedByUserRef.current = false;
          isStreamingRef.current = false;
          streamedLocallyRef.current = false;
          setIsStreaming(false);
          // Stay authoritative until the backend confirms the turn settled. Dropping into viewer
          // mode here re-attaches and replays a snapshot that can race the final durable writes,
          // briefly replacing the finished transcript with a shorter one. The idle transition
          // below is the safe moment to return to viewer mode.
        };

        let sendAccepted = false;
        let idleSnapshotSeen = false;
        const observe = (sessionIdentifier: string) => {
          attachRef.current = attachSession(
            sessionIdentifier,
            (frame) => {
              // The one frame that says the turn ended. The stream does not close: it belongs to the session.
              if (frame.kind === "snapshot") {
                idleSnapshotSeen = frame.reconnected && !frame.running;
                if (sendAccepted && idleSnapshotSeen) finishTurn();
                return;
              }
              if (frame.kind === "turn") {
                if (!frame.running) finishTurn();
                return;
              }
              // A snapshot is catch-up for a viewer. We are driving, so replacing state would drop the sent message.
              if (frame.kind === "delta") {
                reduceLiveDelta(stateRef.current, frame);
                flush();
                return;
              }
              if (frame.kind !== "live") return;
              notifyTurnError(frame.part);
              reduceAgentPart(stateRef.current, frame.part);
              flush();
            },
            finishTurn,
          );
        };

        // Drawn in the same tick the outbox stopped drawing its card, so it is in exactly one place.
        showOptimistically();

        // A turn is two calls: ensure a session, then send. `send` carries no settings and cannot change them.
        void (async () => {
          try {
            let sessionIdentifier = sessionIdRef.current;
            if (!sessionIdentifier) {
              const created = await sessionCreate({
                agent,
                workingDirectory,
                worktreeStrategy,
                permissionMode,
                workspaceId,
              });
              sessionIdentifier = created.id;
              sessionIdRef.current = created.id;
              viewerContextRef.current = created.id;
              setSessionId(created.id);
              // The mode the session actually got, which a parent session may have constrained.
              if (created.permission_mode && created.permission_mode !== permissionMode) {
                setGrantedPermissionMode(created.permission_mode);
              }
            }
            // Attach before sending, or the opening frames are missed.
            observe(sessionIdentifier);
            // The server acknowledges the installed subscription before a send may create any event.
            if (!(await attachRef.current?.ready)) {
              throw new Error("The live transcript could not be attached.");
            }
            const outcome = await sessionSend(sessionIdentifier, messageParts(text, dataParts), {
              messageId: userMessageId,
            });
            // Refused because the session is parked: nothing was delivered, and the message stays in the queue.
            if (!outcome.accepted) {
              withdrawOptimistic();
              if (outcome.compactionRequired) {
                finishTurn();
                settleDelivery("compaction");
                return;
              }
              notifyHeldForDecision(outcome.waitingOn);
              finishTurn();
              settleDelivery("refused");
              return;
            }
            sendAccepted = true;
            if (idleSnapshotSeen) finishTurn();
            settleDelivery("accepted");
          } catch (caught) {
            // What was thrown goes to telemetry as its own fields, where a name and a stack stay searchable.
            swallowed({ component: "chat", operation: "start the turn" }, caught);
            // Never delivered, so the row goes and the queued card is again the only place it is shown.
            withdrawOptimistic();
            pushErrorMessage(stateRef.current, { code: "server_error" });
            // One wind-down for every ending. `finishTurn` closes the stream if one was opened.
            finishTurn();
            // It never reached the session, so the message keeps its place and nothing retries it.
            settleDelivery("failed");
          }
        })();
      }),
    [
      agent,
      workingDirectory,
      worktreeStrategy,
      permissionMode,
      workspaceId,
      flush,
      flushNow,
      notifyTurnError,
      notifyHeldForDecision,
    ],
  );

  useEffect(() => {
    startTurnRef.current = startTurn;
  }, [startTurn]);

  // Hand one message to the session whatever state it is in, and answer what became of it.
  const deliver = useCallback(
    async (message: OutboxMessage): Promise<Delivery> => {
      const context = sessionIdRef.current;
      if (!isStreamingRef.current || !context) return startTurnRef.current(message);
      const key = stableMessageId(stateRef.current, "user", message.id);
      try {
        // Drawn before the send, or anything the session says lands above it.
        upsertMessage(stateRef.current, {
          id: key,
          role: "user",
          content: message.text,
          timestamp: new Date().toISOString(),
        });
        // Synchronously, so the chip and the transcript row hand over as a move rather than a flicker.
        flushNow();
        const outcome = await sessionSend(context, messageParts(message.text, message.dataParts), {
          messageId: message.id,
        });
        if (!outcome.accepted) {
          dropMessage(stateRef.current, key);
          flushNow();
          if (outcome.compactionRequired) return "compaction";
          notifyHeldForDecision(outcome.waitingOn);
          return "refused";
        }
        return "accepted";
      } catch {
        // Never delivered, so the row goes and the queued card is again the only place it is shown.
        dropMessage(stateRef.current, key);
        flushNow();
        return "failed";
      }
    },
    [notifyHeldForDecision, flushNow],
  );

  // The queue, and the only thing that empties it. A ref, because it outlives every render.
  const deliverRef = useRef(deliver);
  useEffect(() => {
    deliverRef.current = deliver;
  }, [deliver]);
  const parkedRef = useRef(hasPendingDecision);
  useEffect(() => {
    parkedRef.current = hasPendingDecision;
  }, [hasPendingDecision]);
  const outboxRef = useRef<Outbox | null>(null);
  useEffect(() => {
    if (outboxRef.current) return;
    outboxRef.current = new Outbox({
      deliver: (message) => deliverRef.current(message),
      parked: () => parkedRef.current(),
      changed: (state) => {
        setQueuedMessages(state.messages);
        setOutboxHold(state.hold);
        setDeliveringMessage(state.delivering);
      },
    });
  }, []);

  // Which conversation the queue belongs to. `retarget`, not a cleanup that fires on every id change.
  useEffect(() => {
    outboxRef.current?.retarget(initialSessionId ?? "");
  }, [initialSessionId]);

  const send = useCallback((text: string, dataParts: Record<string, unknown>[] = []) => {
    const trimmed = text.trim();
    if (!trimmed) return Promise.resolve();
    // Every message goes in the same way, whatever the session is doing. No branch at the keystroke.
    return (
      outboxRef.current?.add({ id: clientIdentifier(), text: trimmed, dataParts }) ??
      Promise.resolve<Delivery>("failed")
    );
  }, []);

  // The held decision was answered, by anyone. The only retry trigger, and one a delivery cannot cause.
  const decisionOpen = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required",
  );
  const decisionWasOpenRef = useRef(decisionOpen);
  useEffect(() => {
    const answered = decisionWasOpenRef.current && !decisionOpen;
    decisionWasOpenRef.current = decisionOpen;
    if (answered) outboxRef.current?.released();
  }, [decisionOpen]);

  // Settle a stuck card and say so when a decision could not be delivered, or the composer stays blocked.
  const notifyResolveFailure = useCallback(
    (requestId: string, kind: "decision" | "answer", status: string) => {
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const permission = message.meta?.permission;
        const question = message.meta?.question;
        if (
          message.role !== "tool_call" ||
          (permission?.requestId !== requestId && question?.requestId !== requestId)
        )
          return message;
        return { ...message, meta: { ...message.meta, status: "failed" } };
      });
      flush();
      toaster.create({
        type: "error",
        title: translation(kind === "decision" ? "decisionFailedTitle" : "answerFailedTitle"),
        description: translation(status === "network" ? "networkBody" : "inactiveBody"),
        closable: true,
      });
    },
    [flush, translation],
  );

  const settleInactivePrompt = useCallback(
    (requestId: string) => {
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const permission = message.meta?.permission;
        const question = message.meta?.question;
        if (
          message.role !== "tool_call" ||
          (permission?.requestId !== requestId && question?.requestId !== requestId)
        )
          return message;
        return { ...message, meta: { ...message.meta, status: "completed" } };
      });
      flush();
    },
    [flush],
  );

  // A decision here is per call: widening what a session may do is the permission mode's job.
  const handlePermission = useCallback(
    async (requestId: string, decision: "deny" | "allow_once") => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolvePermission(context, requestId, decision);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "decision", result.status);
        return;
      }
      // Record the decision and hand the card back to its lifecycle; the result or the error finalizes it.
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const permission = message.meta?.permission;
        if (message.role !== "tool_call" || permission?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", permission: { ...permission, decision } },
        };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt],
  );

  const handleQuestion = useCallback(
    async (requestId: string, answers: QuestionAnswer[]) => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolveQuestion(context, requestId, answers);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "answer", result.status);
        return;
      }
      // Record the answer and hand the card back to its running lifecycle.
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const question = message.meta?.question;
        if (message.role !== "tool_call" || question?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", question: { ...question, answers } },
        };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt],
  );

  // Dismissed without answering: tell the model, which stops the turn, and settle the card. Not a Stop.
  const declineQuestion = useCallback(
    async (requestId: string) => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolveQuestion(context, requestId, [], true);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "answer", result.status);
        return;
      }
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const question = message.meta?.question;
        if (message.role !== "tool_call" || question?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "completed", question: { ...question, declined: true } },
        };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt],
  );

  const abort = useCallback(() => {
    const context = sessionIdRef.current;
    // Suppress the auto-drain the imminent close would trigger, so Stop does not relaunch a queued follow-up.
    abortedByUserRef.current = true;
    // Retire the steering chips now: a message in flight is a chip until the session injects it.
    if (!context) {
      // The session was never created, so there is nothing to cancel but the open stream.
      attachRef.current?.abort();
      return Promise.resolve();
    }
    // A Stop while parked settles the decision, so nothing is left hanging at "input required".
    let settledAny = false;
    for (const message of stateRef.current.messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const permission = message.meta?.permission;
      const question = message.meta?.question;
      if (permission?.requestId) {
        settledAny = true;
        void resolvePermission(context, permission.requestId, "deny");
      } else if (question?.requestId) {
        settledAny = true;
        // A Stop settles an open question as a decline, so the awaiting tool resolves cleanly.
        void resolveQuestion(context, question.requestId, [], true);
      }
    }
    if (settledAny) {
      stateRef.current.messages = stateRef.current.messages.map((message) =>
        message.role === "tool_call" && message.meta?.status === "input_required"
          ? { ...message, meta: { ...message.meta, status: "failed" } }
          : message,
      );
      flush();
    }
    // Say so if the stop never reached the server: the turn may still be running.
    return cancelTurn(context).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("stopFailedTitle"),
          description: translation("stopFailedBody"),
          closable: true,
        });
      }
    });
  }, [flush, translation]);

  // Kick off a manual compaction; the local marker covers passes that finish between stream subscriptions.
  const compact = useCallback(() => {
    const context = sessionIdRef.current;
    if (!context) return;
    if (
      stateRef.current.messages.some(
        (message) => message.role === "compaction" && message.meta?.status === "running",
      )
    )
      return;
    const failed = stateRef.current.messages.findLast(
      (message) => message.role === "compaction" && message.meta?.status === "failed",
    );
    const markerId = failed?.id ?? `compaction-request-${clientIdentifier()}`;
    const nextMarker: ChatMessage = {
      id: markerId,
      role: "compaction",
      content: "",
      timestamp: new Date().toISOString(),
      meta: { status: "running", reason: "manual" },
    };
    if (failed) upsertMessage(stateRef.current, nextMarker);
    else stateRef.current.appendMessage(nextMarker);
    flushNow();
    void compactSession(context)
      .then(async (result) => {
        const failed =
          result.compacted === false || result.status !== "done" || result.ok === false;
        // The persisted transcript is authoritative: it carries the compaction events the
        // backend wrote, with the exact reason, counts and error code, and removes whatever
        // history the fold actually reclaimed. Replaying it also settles the local marker
        // under the transcript's own identity instead of drawing a duplicate separator.
        try {
          const turns = await fetchSessionTurns(context);
          const replayed = replayTurns(turns);
          const settled = replayed.messages.some(
            (message) =>
              message.role === "compaction" &&
              (message.meta?.status === "done" || message.meta?.status === "failed"),
          );
          if (failed && !settled) {
            // The failure never reached the backend, so replaying would erase the marker
            // rather than settle it. Keep the local row and mark it failed.
            settleCompactionMarker(stateRef.current, markerId, {
              status: "failed",
              compactionErrorCode: result.error_code ?? "compaction_failed",
            });
          } else {
            stateRef.current = replayed;
          }
          flushNow();
        } catch (caught) {
          swallowed({ component: "chat", operation: "read the compacted transcript" }, caught);
          if (failed) {
            settleCompactionMarker(stateRef.current, markerId, {
              status: "failed",
              compactionErrorCode: result.error_code ?? "compaction_failed",
            });
          } else {
            const changed = (result.messages_after ?? 0) < (result.messages_before ?? 0);
            if (changed) {
              settleCompactionMarker(stateRef.current, markerId, {
                status: "done",
                reason: result.reason ?? "manual",
                messagesBefore: result.messages_before ?? 0,
                messagesAfter: result.messages_after ?? 0,
              });
            } else {
              dropCompactionMarker(stateRef.current, markerId);
            }
          }
          flushNow();
        }
        if (failed) {
          toaster.create({
            type: "error",
            title: translation("compactFailedTitle"),
            description: translation("compactFailedBody"),
            closable: true,
          });
        } else {
          // Only a successful fold releases the queue that the failed one held.
          outboxRef.current?.compactionRecovered();
        }
      })
      .catch((caught) => {
        swallowed({ component: "chat", operation: "compact the conversation" }, caught);
        settleCompactionMarker(stateRef.current, markerId, {
          status: "failed",
          compactionErrorCode: "compaction_failed",
        });
        flushNow();
        toaster.create({
          type: "error",
          title: translation("compactFailedTitle"),
          description: translation("compactFailedBody"),
          closable: true,
        });
      });
  }, [flushNow, translation]);

  const abortTool = useCallback(
    (toolCallId: string) => {
      const context = sessionIdRef.current;
      if (!context || !toolCallId) return;
      void abortToolCall(context, toolCallId).then((ok) => {
        if (!ok) {
          toaster.create({
            type: "error",
            title: translation("cancelToolFailedTitle"),
            description: translation("cancelToolFailedBody"),
            closable: true,
          });
        }
      });
      stateRef.current.messages = stateRef.current.messages.map((message) =>
        message.role === "tool_call" && message.meta?.toolCallId === toolCallId
          ? { ...message, meta: { ...message.meta, status: "failed" } }
          : message,
      );
      flush();
    },
    [flush, translation],
  );

  const dequeueMessage = useCallback(
    (index: number) => {
      const target = queuedMessages[index];
      if (target) outboxRef.current?.remove(target.id);
    },
    [queuedMessages],
  );

  /** Ask again after a failure to reach the session. The person's own retry. */
  const retryOutbox = useCallback(() => outboxRef.current?.retry(), []);

  const retryTurn = useCallback(async (): Promise<boolean> => {
    const context = sessionIdRef.current;
    if (!context || isStreamingRef.current) return false;
    const errorIndex = stateRef.current.messages.findLastIndex(
      (message) => message.role === "error",
    );
    if (errorIndex < 0) return false;
    stateRef.current.messages = stateRef.current.messages.map((message, index) =>
      index === errorIndex
        ? { ...message, meta: { ...message.meta, retrying: true, retryFailed: false } }
        : message,
    );
    flushNow();
    isStreamingRef.current = true;
    streamedLocallyRef.current = true;
    setIsStreaming(true);
    let retryAccepted = false;
    let idleSnapshotSeen = false;
    const observe = attachSession(
      context,
      (frame) => {
        if (frame.kind === "snapshot") {
          idleSnapshotSeen = frame.reconnected && !frame.running;
          if (retryAccepted && idleSnapshotSeen) {
            observe.abort();
            isStreamingRef.current = false;
            streamedLocallyRef.current = false;
            setIsStreaming(false);
          }
          return;
        }
        if (frame.kind === "turn" && !frame.running) {
          observe.abort();
          isStreamingRef.current = false;
          streamedLocallyRef.current = false;
          setIsStreaming(false);
          return;
        }
        if (frame.kind === "live") {
          reduceAgentPart(stateRef.current, frame.part);
          flush();
        } else if (frame.kind === "delta") {
          reduceLiveDelta(stateRef.current, frame);
          flush();
        }
      },
      () => {
        isStreamingRef.current = false;
        streamedLocallyRef.current = false;
        setIsStreaming(false);
      },
    );
    if (!(await observe.ready)) {
      isStreamingRef.current = false;
      streamedLocallyRef.current = false;
      setIsStreaming(false);
      return false;
    }
    const accepted = await retrySessionTurn(context);
    retryAccepted = accepted;
    if (retryAccepted && idleSnapshotSeen) {
      observe.abort();
      isStreamingRef.current = false;
      streamedLocallyRef.current = false;
      setIsStreaming(false);
    }
    if (!accepted) {
      observe.abort();
      isStreamingRef.current = false;
      streamedLocallyRef.current = false;
      setIsStreaming(false);
      stateRef.current.messages = stateRef.current.messages.map((message, index) =>
        index === errorIndex
          ? { ...message, meta: { ...message.meta, retrying: false, retryFailed: true } }
          : message,
      );
      flushNow();
    }
    return accepted;
  }, [flush, flushNow]);

  const reset = useCallback(() => {
    abort();
    stateRef.current = newReduceState();
    setMessages([]);
    setTasks([]);
    setTokenUsage(null);
    setSessionId(null);
    sessionIdRef.current = null;
    setIsHistoryStreaming(false);
  }, [abort]);

  return {
    messages,
    tasks,
    tokenUsage,
    queuedMessages,
    sessionId,
    isStreaming: isStreaming || sessionRunning,
    isHistoryLoading,
    isHistoryStreaming,
    historyError,
    reloadHistory,
    send,
    abort,
    abortTool,
    compact,
    reset,
    dequeueMessage,
    outboxHold,
    deliveringMessage,
    grantedPermissionMode,
    retryOutbox,
    retryTurn,
    handlePermission,
    handleQuestion,
    declineQuestion,
  };
}
