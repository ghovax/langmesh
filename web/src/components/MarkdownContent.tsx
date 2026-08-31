"use client";

import {
  Box,
  Code,
  Heading,
  Image,
  Link,
  List,
  Separator,
  Span,
  Table,
  Text,
} from "@chakra-ui/react";
import {
  Children,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { visit } from "unist-util-visit";
import twemoji from "@twemoji/api";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { xcode, atomOneDark } from "react-syntax-highlighter/dist/esm/styles/hljs";
import type { Components } from "react-markdown";
import type { Root } from "mdast";
import { useReducedMotion } from "motion/react";
// flowtoken's public component cannot host our own pipeline, so its token animator is used directly.
import SplitText from "flowtoken/dist/components/SplitText";
import { useColorModeValue } from "./ui/ColorMode";
import { MermaidDiagram } from "./MermaidDiagram";
import { DeletedText, Emphasis, Strong } from "./ui/Semantic";

interface MarkdownContentProps {
  content: string;
  // Base font size for body text, inherited from the wrapper so callers can match their context.
  fontSize?: string;
  // Animate newly-streamed text, set only for the one live message.
  animate?: boolean;
  // Link plain GitHub-style mentions when the content is known to come from GitHub.
  linkGitHubMentions?: boolean;
}

// Vertical rhythm of the prose, kept compact because the transcript is a working document.
const blockGap = "0.625rem";
const headingGap = "0.375rem";
const sectionGap = "0.875rem";
const variationSelectorPattern = /\uFE0F/g;
const zeroWidthJoiner = String.fromCharCode(0x200d);
const emojiMarkerPattern = /\uE000(\d+)\uE001/g;
const decimalDigitPattern = /\p{Decimal_Number}/u;
const amountAtStartPattern = /^\s*\d[\d,.]*(?:\s|$)/;
const explicitMathSyntaxPattern = /[\\^_={}<>+*/]/;
const githubMentionPattern = /(^|[^\w@-])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/g;
const faviconSize = "0.875em";

function remarkLinkGitHubMentions() {
  return (tree: Root) => {
    visit(tree, "text", (node, nodeIndex, parent) => {
      if (
        nodeIndex === undefined ||
        !parent ||
        parent.type === "link" ||
        parent.type === "linkReference"
      ) {
        return;
      }
      const text = node.value;
      githubMentionPattern.lastIndex = 0;
      if (!githubMentionPattern.test(text)) return;
      githubMentionPattern.lastIndex = 0;
      const replacement: typeof parent.children = [];
      let previousEnd = 0;
      for (const match of text.matchAll(githubMentionPattern)) {
        const matchStart = match.index ?? 0;
        const prefix = match[1] ?? "";
        const username = match[2] ?? "";
        const mentionStart = matchStart + prefix.length;
        if (mentionStart > previousEnd) {
          replacement.push({ type: "text", value: text.slice(previousEnd, mentionStart) });
        }
        replacement.push({
          type: "link",
          title: null,
          url: `https://github.com/${username}`,
          children: [{ type: "text", value: `@${username}` }],
        });
        previousEnd = mentionStart + username.length + 1;
      }
      if (previousEnd < text.length) {
        replacement.push({ type: "text", value: text.slice(previousEnd) });
      }
      parent.children.splice(nodeIndex, 1, ...replacement);
    });
  };
}

function faviconSource(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return `https://icons.duckduckgo.com/ip3/${url.hostname}.ico`;
  } catch {
    return null;
  }
}

function FaviconLink({ href, children }: { href?: string; children: ReactNode }) {
  const source = faviconSource(href);
  const [showFavicon, setShowFavicon] = useState(source !== null);
  return (
    <Link
      href={href}
      colorPalette="blue"
      fontSize="inherit"
      textUnderlineOffset="2px"
      textDecorationThickness="1px"
      target="_blank"
      rel="noopener noreferrer"
      display="inline-flex"
      alignItems="center"
      gap="0.25em"
      verticalAlign="middle"
    >
      {showFavicon && source ? (
        <Image
          src={source}
          alt=""
          aria-hidden="true"
          boxSize={faviconSize}
          flexShrink={0}
          objectFit="contain"
          loading="lazy"
          onError={() => setShowFavicon(false)}
        />
      ) : null}
      {children}
    </Link>
  );
}

function preserveCurrencyDollars() {
  return (tree: Root, file: { value: unknown }) => {
    const markdownSource = String(file.value);
    visit(tree, "inlineMath", (node, nodeIndex, parent) => {
      if (nodeIndex === undefined || !parent || !node.position) return;
      const startOffset = node.position.start.offset;
      const endOffset = node.position.end.offset;
      if (startOffset === undefined || endOffset === undefined) return;
      const originalText = markdownSource.slice(startOffset, endOffset);
      if (originalText.startsWith("$$")) return;
      const dollarTouchesExternalAmount =
        decimalDigitPattern.test(markdownSource.at(startOffset - 1) ?? "") ||
        decimalDigitPattern.test(markdownSource.at(endOffset) ?? "");
      const beginsWithAmountWithoutMathSyntax =
        amountAtStartPattern.test(node.value) && !explicitMathSyntaxPattern.test(node.value);
      if (!dollarTouchesExternalAmount && !beginsWithAmountWithoutMathSyntax) return;
      parent.children[nodeIndex] = { type: "text", value: originalText };
    });
  };
}

const twemojiStyle: CSSProperties = {
  display: "inline-block",
  width: "1em",
  height: "1em",
  margin: "0 0.05em",
  verticalAlign: "-0.125em",
};

function twemojiCodePoint(rawEmoji: string): string {
  const normalizedEmoji = rawEmoji.includes(zeroWidthJoiner)
    ? rawEmoji
    : rawEmoji.replace(variationSelectorPattern, "");
  return twemoji.convert.toCodePoint(normalizedEmoji);
}

function twemojiSource(rawEmoji: string): string {
  return `${twemoji.base}svg/${twemojiCodePoint(rawEmoji)}.svg`;
}

function handleTwemojiError(event: SyntheticEvent<HTMLImageElement>) {
  twemoji.onerror.call(event.currentTarget);
}

function renderTextWithTwemoji(text: string): ReactNode {
  if (!twemoji.test(text)) return text;

  const emojis: string[] = [];
  const markedText = twemoji.replace(text, (rawEmoji) => {
    if (rawEmoji === "\uFE0F") return rawEmoji;
    const marker = `\uE000${emojis.length}\uE001`;
    emojis.push(rawEmoji);
    return marker;
  });

  if (emojis.length === 0) return markedText;

  const nodes: ReactNode[] = [];
  let previousIndex = 0;
  for (const match of markedText.matchAll(emojiMarkerPattern)) {
    const matchIndex = match.index ?? 0;
    if (matchIndex > previousIndex) {
      nodes.push(markedText.slice(previousIndex, matchIndex));
    }
    const emojiIndex = Number(match[1]);
    const rawEmoji = emojis[emojiIndex];
    nodes.push(
      <Image
        key={`emoji-${matchIndex}-${emojiIndex}`}
        className="twemoji"
        draggable={false}
        alt={rawEmoji}
        src={twemojiSource(rawEmoji)}
        decoding="async"
        loading="lazy"
        style={twemojiStyle}
        onError={handleTwemojiError}
      />,
    );
    previousIndex = matchIndex + match[0].length;
  }
  if (previousIndex < markedText.length) {
    nodes.push(markedText.slice(previousIndex));
  }
  return nodes;
}

function renderEmojiChildren(children: ReactNode): ReactNode {
  return Children.map(children, (child) =>
    typeof child === "string" ? renderTextWithTwemoji(child) : child,
  );
}

// The streaming token animation is paint-only, so layout and shaping are identical before and after.
const TOKEN_ANIMATION = "token-fade-in";
const TOKEN_DURATION = "0.16s";
const TOKEN_TIMING = "cubic-bezier(0.2, 0.8, 0.2, 1)";
// The duration is a custom property rather than a prop, so streaming never reaches a component's identity.
const TOKEN_DURATION_PROPERTY = "--token-duration";
const TOKEN_DURATION_VALUE = `var(${TOKEN_DURATION_PROPERTY}, 0s)`;

function AnimatedText({ text }: { text: string }): ReactNode {
  return Children.toArray(renderTextWithTwemoji(text)).map((segment, segmentIndex) => {
    if (typeof segment !== "string") return segment;
    return (
      <Span className="flowtoken-text-leaf" key={`text-${segmentIndex}`}>
        <SplitText
          input={segment}
          sep="diff"
          animation={TOKEN_ANIMATION}
          animationDuration={TOKEN_DURATION_VALUE}
          animationTimingFunction={TOKEN_TIMING}
          animationIterationCount={1}
        />
      </Span>
    );
  });
}

// Every text leaf keeps the same structure in both states, so settling a turn cannot change wrapping.
function renderChildren(children: ReactNode): ReactNode {
  return Children.map(children, (child) =>
    typeof child === "string" ? <AnimatedText text={child} /> : child,
  );
}

// A single-line `$$...$$` parses as inline math, so a paragraph holding only it is centred here.
interface MarkdownElement {
  children: Array<{
    type: string;
    properties?: { className?: unknown };
  }>;
}

function isDisplayMathParagraph(node: MarkdownElement | undefined): boolean {
  if (!node || node.children.length !== 1) return false;
  const [child] = node.children;
  return (
    child.type === "element" &&
    Array.isArray(child.properties?.className) &&
    child.properties.className.includes("katex")
  );
}

// Coalesce a rapidly-growing string down to a readable rate, since re-parsing per frame is quadratic.
const STREAMING_RENDER_INTERVAL_MS = 66;

function useThrottledText(text: string, intervalMs: number, enabled: boolean): string {
  const [throttled, setThrottled] = useState(text);
  const lastRenderedAt = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!enabled) {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    if (text === throttled) return;
    const elapsed = performance.now() - lastRenderedAt.current;
    const commit = () => {
      lastRenderedAt.current = performance.now();
      setThrottled(text);
    };
    if (elapsed >= intervalMs) {
      commit();
      return;
    }
    timerRef.current = setTimeout(commit, intervalMs - elapsed);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [text, throttled, intervalMs, enabled]);

  return enabled ? throttled : text;
}

export const MarkdownContent = memo(function MarkdownContent({
  content,
  fontSize = "sm",
  animate = false,
  linkGitHubMentions = false,
}: MarkdownContentProps) {
  const syntaxTheme = useColorModeValue(xcode, atomOneDark);
  // Reduced-motion readers keep the stable token structure but receive a zero-duration fade.
  const reduceMotion = useReducedMotion();
  const animating = animate && !reduceMotion;
  // Once a message has animated it keeps its token duration, so settling a turn does not restyle every span.
  const [everAnimated, setEverAnimated] = useState(animating);
  if (animating && !everAnimated) setEverAnimated(true);
  const tokenDuration = everAnimated ? TOKEN_DURATION : "0s";
  const renderedContent = useThrottledText(content, STREAMING_RENDER_INTERVAL_MS, animating);

  const markdownComponents = useMemo<Components>(
    () => ({
      img({ src, alt }) {
        if (!src || (typeof src === "string" && !src.trim())) return null;
        return (
          <Image src={typeof src === "string" ? src : undefined} alt={alt ?? ""} maxW="100%" />
        );
      },
      p({ node, children }) {
        if (isDisplayMathParagraph(node)) {
          return (
            <Box textAlign="center" fontSize="inherit">
              {children}
            </Box>
          );
        }
        return (
          <Text fontSize="inherit" lineHeight="1.55">
            {renderChildren(children)}
          </Text>
        );
      },
      h1({ children }) {
        return (
          <Heading as="h1" fontSize="lg" fontWeight="bold">
            {renderChildren(children)}
          </Heading>
        );
      },
      h2({ children }) {
        return (
          <Heading as="h2" fontSize="md" fontWeight="bold">
            {renderChildren(children)}
          </Heading>
        );
      },
      h3({ children }) {
        return (
          <Heading as="h3" fontSize="sm" fontWeight="bold">
            {renderChildren(children)}
          </Heading>
        );
      },
      h4({ children }) {
        return (
          <Heading as="h4" fontSize="sm" fontWeight="semibold" color="fg.muted">
            {renderChildren(children)}
          </Heading>
        );
      },
      a({ href, children }) {
        return <FaviconLink href={href}>{renderChildren(children)}</FaviconLink>;
      },
      ul({ children }) {
        return (
          <List.Root pl={6} fontSize="inherit" listStyleType="disc">
            {children}
          </List.Root>
        );
      },
      ol({ children }) {
        return (
          <List.Root as="ol" pl={6} fontSize="inherit" listStyleType="decimal">
            {children}
          </List.Root>
        );
      },
      li({ children }) {
        return (
          <List.Item mb={0.5} fontSize="inherit" lineHeight="1.55" _last={{ mb: 0 }}>
            {renderChildren(children)}
          </List.Item>
        );
      },
      blockquote({ children }) {
        // The same rule and padding as an expanded tool call's detail rail, so quoted prose reads as one language.
        return (
          <Box
            borderLeftWidth="2px"
            borderColor="border.muted"
            pl={3}
            py={0.5}
            color="fg.muted"
            fontSize="inherit"
          >
            {renderChildren(children)}
          </Box>
        );
      },
      code({ className, children }) {
        const languageMatch = /language-(\w+)/.exec(className || "");
        const codeString = String(children).replace(/\n$/, "");
        const isBlock = !!languageMatch || codeString.includes("\n");

        if (isBlock) {
          const block = (
            <Box
              borderRadius="md"
              overflow="auto"
              borderWidth="1px"
              borderColor="border.muted"
              fontSize="xs"
              my={1.5}
              maxW="100%"
              maxH="420px"
              bg="bg.subtle"
            >
              <SyntaxHighlighter
                style={syntaxTheme}
                language={languageMatch ? languageMatch[1] : "text"}
                PreTag="div"
                customStyle={{
                  margin: 0,
                  padding: "0.5rem 0.625rem",
                  lineHeight: 1.5,
                  borderRadius: "var(--chakra-radii-none)",
                  fontFamily: "var(--app-font-mono)",
                  fontSize: "inherit",
                  background: "var(--chakra-colors-bg-subtle)",
                  minWidth: "max-content",
                }}
                codeTagProps={{
                  style: { fontFamily: "inherit", fontSize: "inherit", whiteSpace: "pre" },
                }}
              >
                {codeString}
              </SyntaxHighlighter>
            </Box>
          );
          // A mermaid fence renders as a diagram once its source parses, with the code block as the fallback.
          if (languageMatch?.[1] === "mermaid") {
            return <MermaidDiagram code={codeString} fallback={block} />;
          }
          return block;
        }

        // A subtle chip with no vertical padding, so inline code does not disturb the line rhythm.
        return (
          <Code
            fontFamily="var(--app-font-mono)"
            px={1.5}
            py={0}
            borderRadius="sm"
            borderWidth="1px"
            borderColor="border.muted"
            bg="bg.subtle"
            color="fg"
          >
            {children}
          </Code>
        );
      },
      pre({ children }) {
        return <>{children}</>;
      },
      // A table with real cells: an outer frame, a filled header, and rules between every row and column.
      table({ children }) {
        return (
          <Box
            overflowX="auto"
            maxW="100%"
            my={3}
            borderWidth="1px"
            borderColor="border"
            borderRadius="md"
          >
            <Table.Root
              variant="outline"
              showColumnBorder
              size="sm"
              minW="100%"
              fontSize="inherit"
              lineHeight="1.55"
              boxShadow="none"
            >
              {renderChildren(children)}
            </Table.Root>
          </Box>
        );
      },
      // Mapped rather than left raw, since the header fill and row rules hang off these two slots.
      thead({ children }) {
        return <Table.Header>{renderChildren(children)}</Table.Header>;
      },
      tbody({ children }) {
        return <Table.Body>{renderChildren(children)}</Table.Body>;
      },
      tr({ children }) {
        return <Table.Row>{renderChildren(children)}</Table.Row>;
      },
      th({ children }) {
        return (
          <Table.ColumnHeader fontWeight="semibold" verticalAlign="top" overflowWrap="break-word">
            {renderChildren(children)}
          </Table.ColumnHeader>
        );
      },
      td({ children }) {
        return (
          <Table.Cell verticalAlign="top" overflowWrap="break-word">
            {renderChildren(children)}
          </Table.Cell>
        );
      },
      hr() {
        return <Separator borderColor="border" my={4} />;
      },
      strong({ children }) {
        return (
          <Strong fontSize="inherit" fontWeight="bold">
            {renderChildren(children)}
          </Strong>
        );
      },
      em({ children }) {
        return (
          <Emphasis fontSize="inherit" fontStyle="italic">
            {renderChildren(children)}
          </Emphasis>
        );
      },
      del({ children }) {
        return <DeletedText fontSize="inherit">{renderChildren(children)}</DeletedText>;
      },
    }),
    [syntaxTheme],
  );

  return (
    <Box
      minW={0}
      fontSize={fontSize}
      // The only thing that changes when a turn settles, so everything below keeps its identity.
      style={{ [TOKEN_DURATION_PROPERTY]: tokenDuration } as CSSProperties}
      css={{
        "& > *": {
          marginBlock: 0,
        },
        "& > * + *": {
          marginBlockStart: blockGap,
        },
        "& > * + :is(h1, h2, h3, h4)": {
          marginBlockStart: sectionGap,
        },
        "& > :is(h1, h2, h3, h4) + *": {
          marginBlockStart: headingGap,
        },
        "& li > p": {
          marginBlock: 0,
        },
        "& li > p + p, & li > ul, & li > ol": {
          marginBlockStart: headingGap,
        },
        // Inline code is sized relative to its surrounding text, since monospace runs visually larger.
        "& :not(pre) > code": {
          fontSize: "0.9125em",
        },
        "& strong, & em, & a": {
          fontSize: "inherit",
        },
        "& code, & pre, & kbd, & samp": {
          fontFamily: "var(--app-font-mono)",
        },
        "& .twemoji": {
          display: "inline-block",
          width: "1em",
          height: "1em",
          margin: "0 0.05em",
          verticalAlign: "-0.125em",
        },
        "& .flowtoken-text-leaf > span": {
          display: "inline !important",
          whiteSpace: "inherit !important",
        },
      }}
    >
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          [remarkMath, { singleDollarTextMath: true }],
          preserveCurrencyDollars,
          ...(linkGitHubMentions ? [remarkLinkGitHubMentions] : []),
        ]}
        rehypePlugins={[[rehypeKatex, { strict: false }]]}
        components={markdownComponents}
      >
        {renderedContent}
      </ReactMarkdown>
    </Box>
  );
});

// A single-line renderer for places where a label must stay inline, collapsing every block to its children.
const passthrough = ({ children }: { children?: ReactNode }) => (
  <>{renderEmojiChildren(children)}</>
);

const inlineMarkdownComponents: Components = {
  p: passthrough,
  h1: passthrough,
  h2: passthrough,
  h3: passthrough,
  h4: passthrough,
  h5: passthrough,
  h6: passthrough,
  ul: passthrough,
  ol: passthrough,
  li: passthrough,
  pre: passthrough,
  blockquote: passthrough,
  hr: () => null,
  img: () => null,
  br: () => <> </>,
  a({ href, children }) {
    return (
      <FaviconLink href={typeof href === "string" ? href : undefined}>
        {renderEmojiChildren(children)}
      </FaviconLink>
    );
  },
  code({ children }) {
    return (
      <Code
        fontFamily="var(--app-font-mono)"
        fontSize="0.9em"
        px={1.5}
        py={0}
        borderRadius="sm"
        borderWidth="1px"
        borderColor="border.muted"
        bg="bg.subtle"
        color="fg"
        whiteSpace="nowrap"
      >
        {children}
      </Code>
    );
  },
  strong({ children }) {
    return (
      <Strong fontSize="inherit" fontWeight="bold">
        {renderEmojiChildren(children)}
      </Strong>
    );
  },
  em({ children }) {
    return (
      <Emphasis fontSize="inherit" fontStyle="italic">
        {renderEmojiChildren(children)}
      </Emphasis>
    );
  },
  del({ children }) {
    return <DeletedText fontSize="inherit">{renderEmojiChildren(children)}</DeletedText>;
  },
};

export const InlineMarkdown = memo(function InlineMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[
        remarkGfm,
        [remarkMath, { singleDollarTextMath: true }],
        preserveCurrencyDollars,
      ]}
      rehypePlugins={[[rehypeKatex, { strict: false }]]}
      components={inlineMarkdownComponents}
    >
      {content}
    </ReactMarkdown>
  );
});
