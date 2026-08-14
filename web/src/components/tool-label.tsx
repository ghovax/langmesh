"use client";

// The inline label in a tool-call heading: the model's own explanation, rendered as the Markdown it is.

import { getToolCallDisplay } from "@/lib/glyphs";
import { InlineMarkdown } from "./markdown-content";
import { useTranslations } from "next-intl";

export function ToolCallLabel({
  name,
  args,
  ready = false,
}: {
  name: string;
  args?: Record<string, unknown>;
  ready?: boolean;
}) {
  const translation = useTranslations("ToolCall");
  const { label } = getToolCallDisplay(name, args, ready);
  if (label) return <InlineMarkdown content={label} />;
  // The reviewer's verdict is the one call with no model-supplied explanation, so its own line names it.
  if (name === "submit_goal_review") {
    return <InlineMarkdown content={translation("submittingVerdict")} />;
  }
  return null;
}
