// v1.0.0 - 2026-04-24 - MarkdownEditor TipTap component (view/raw/wysiwyg)
"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import Table from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import { Markdown } from "tiptap-markdown";
import SafeMarkdown from "@/components/projects/SafeMarkdown";

const CodeEditor = dynamic(() => import("@/components/finder/viewers/CodeEditor"), {
  ssr: false,
  loading: () => <div className="flex-1 bg-pir-base animate-pulse" />,
});

export type MarkdownEditorMode = "view" | "raw" | "wysiwyg";

interface MarkdownEditorProps {
  content: string;
  readOnly?: boolean;
  onChange?: (markdown: string) => void;
  onSave?: (markdown: string) => void;
  defaultMode?: MarkdownEditorMode;
}

const MODES: { id: MarkdownEditorMode; label: string }[] = [
  { id: "view", label: "View" },
  { id: "raw", label: "Raw" },
  { id: "wysiwyg", label: "WYSIWYG" },
];

const TOOLBAR_BTN =
  "h-7 px-2 text-caption rounded border border-transparent hover:border-pir-border hover:bg-pir-surface-2 transition-colors text-pir-text-secondary";
const TOOLBAR_BTN_ACTIVE =
  "h-7 px-2 text-caption rounded border border-pir-accent/40 bg-pir-accent/15 text-pir-accent transition-colors";

export default function MarkdownEditor({
  content,
  readOnly = false,
  onChange,
  onSave,
  defaultMode = "view",
}: MarkdownEditorProps) {
  const [mode, setMode] = useState<MarkdownEditorMode>(defaultMode);
  const [markdownState, setMarkdownState] = useState<string>(content);
  // Track latest markdown via ref so global Cmd+S handler always sees fresh value.
  const markdownRef = useRef<string>(content);
  // Suppress onUpdate -> onChange callback when we set content programmatically (mode swap).
  const programmaticUpdateRef = useRef(false);

  // Keep state in sync if parent changes content (e.g. opens different file).
  useEffect(() => {
    setMarkdownState(content);
    markdownRef.current = content;
  }, [content]);

  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  const editor = useEditor(
    {
      extensions: [
        StarterKit.configure({
          heading: { levels: [1, 2, 3, 4] },
        }),
        Link.configure({
          openOnClick: false,
          autolink: true,
          HTMLAttributes: {
            rel: "noopener noreferrer nofollow",
            target: "_blank",
          },
        }),
        Table.configure({ resizable: true, allowTableNodeSelection: true }),
        TableRow,
        TableCell,
        TableHeader,
        Markdown.configure({
          html: false,
          tightLists: true,
          bulletListMarker: "-",
          linkify: false,
          breaks: false,
          transformPastedText: true,
        }),
      ],
      content: markdownState,
      editable: !readOnly && mode === "wysiwyg",
      immediatelyRender: false,
      onUpdate: ({ editor: ed }) => {
        if (programmaticUpdateRef.current) return;
        // tiptap-markdown exposes getMarkdown() via storage.markdown
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const md = (ed.storage as any).markdown.getMarkdown() as string;
        markdownRef.current = md;
        setMarkdownState(md);
        onChangeRef.current?.(md);
      },
    },
    [],
  );

  // Sync editable flag when readOnly/mode changes.
  useEffect(() => {
    if (!editor) return;
    editor.setEditable(!readOnly && mode === "wysiwyg");
  }, [editor, readOnly, mode]);

  // When entering wysiwyg, push the latest markdown into TipTap.
  // When leaving wysiwyg, pull TipTap's markdown back into state.
  const previousModeRef = useRef<MarkdownEditorMode>(defaultMode);
  useEffect(() => {
    if (!editor) {
      previousModeRef.current = mode;
      return;
    }
    const prev = previousModeRef.current;
    if (prev !== "wysiwyg" && mode === "wysiwyg") {
      programmaticUpdateRef.current = true;
      editor.commands.setContent(markdownRef.current, false);
      // ProseMirror dispatch is sync; release flag on next tick to be safe.
      queueMicrotask(() => {
        programmaticUpdateRef.current = false;
      });
    } else if (prev === "wysiwyg" && mode !== "wysiwyg") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const md = (editor.storage as any).markdown.getMarkdown() as string;
      markdownRef.current = md;
      setMarkdownState(md);
    }
    previousModeRef.current = mode;
  }, [mode, editor]);

  // Cmd/Ctrl+S handler bound to onSave with the freshest markdown.
  useEffect(() => {
    if (!onSave) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        onSave(markdownRef.current);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onSave]);

  // Raw-mode handlers — CodeEditor passes the value on every keystroke.
  const handleRawChange = useCallback((next?: string) => {
    if (typeof next !== "string") return;
    markdownRef.current = next;
    setMarkdownState(next);
    onChangeRef.current?.(next);
  }, []);
  const handleRawSave = useCallback(
    (next: string) => {
      markdownRef.current = next;
      setMarkdownState(next);
      onChangeRef.current?.(next);
      onSave?.(next);
    },
    [onSave],
  );

  const wysiwygActions = useMemo(() => {
    if (!editor) return null;
    const isActive = (name: string, attrs?: Record<string, unknown>) =>
      editor.isActive(name, attrs);
    const cls = (active: boolean) => (active ? TOOLBAR_BTN_ACTIVE : TOOLBAR_BTN);
    const promptLink = () => {
      const previous = editor.getAttributes("link").href as string | undefined;
      const url = window.prompt("URL", previous ?? "https://");
      if (url === null) return;
      if (url === "") {
        editor.chain().focus().extendMarkRange("link").unsetLink().run();
        return;
      }
      editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
    };
    return (
      <div className="flex items-center gap-1 flex-wrap">
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
          className={cls(isActive("heading", { level: 1 }))}
          aria-label="Heading 1"
          title="Heading 1"
        >
          H1
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
          className={cls(isActive("heading", { level: 2 }))}
          aria-label="Heading 2"
          title="Heading 2"
        >
          H2
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
          className={cls(isActive("heading", { level: 3 }))}
          aria-label="Heading 3"
          title="Heading 3"
        >
          H3
        </button>
        <span className="w-px h-4 bg-pir-border mx-1" aria-hidden />
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleBold().run()}
          className={cls(isActive("bold"))}
          aria-label="Bold"
          title="Bold (Cmd+B)"
        >
          <span className="font-semibold">B</span>
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleItalic().run()}
          className={cls(isActive("italic"))}
          aria-label="Italic"
          title="Italic (Cmd+I)"
        >
          <span className="italic">I</span>
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleStrike().run()}
          className={cls(isActive("strike"))}
          aria-label="Strikethrough"
          title="Strikethrough"
        >
          <span className="line-through">S</span>
        </button>
        <span className="w-px h-4 bg-pir-border mx-1" aria-hidden />
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleBulletList().run()}
          className={cls(isActive("bulletList"))}
          aria-label="Bullet list"
          title="Bullet list"
        >
          {"•"} List
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleOrderedList().run()}
          className={cls(isActive("orderedList"))}
          aria-label="Ordered list"
          title="Ordered list"
        >
          1. List
        </button>
        <span className="w-px h-4 bg-pir-border mx-1" aria-hidden />
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleCode().run()}
          className={cls(isActive("code"))}
          aria-label="Inline code"
          title="Inline code"
        >
          {"<>"}
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleCodeBlock().run()}
          className={cls(isActive("codeBlock"))}
          aria-label="Code block"
          title="Code block"
        >
          {"{ }"}
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().toggleBlockquote().run()}
          className={cls(isActive("blockquote"))}
          aria-label="Blockquote"
          title="Blockquote"
        >
          {"“”"}
        </button>
        <span className="w-px h-4 bg-pir-border mx-1" aria-hidden />
        <button
          type="button"
          onClick={promptLink}
          className={cls(isActive("link"))}
          aria-label="Link"
          title="Add or edit link"
        >
          Link
        </button>
        <button
          type="button"
          onClick={() =>
            editor
              .chain()
              .focus()
              .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
              .run()
          }
          className={TOOLBAR_BTN}
          aria-label="Insert table"
          title="Insert 3x3 table"
        >
          Table
        </button>
        <button
          type="button"
          onClick={() => editor.chain().focus().setHorizontalRule().run()}
          className={TOOLBAR_BTN}
          aria-label="Horizontal rule"
          title="Horizontal rule"
        >
          {"──"}
        </button>
        {onSave ? (
          <>
            <span className="w-px h-4 bg-pir-border mx-1" aria-hidden />
            <button
              type="button"
              onClick={() => onSave(markdownRef.current)}
              className="h-7 px-2 text-caption rounded border border-pir-accent/40 bg-pir-accent/15 text-pir-accent hover:bg-pir-accent/25 transition-colors"
              aria-label="Save"
              title="Save (Cmd+S)"
            >
              Save
            </button>
          </>
        ) : null}
      </div>
    );
  }, [editor, onSave]);

  return (
    <div
      className="md-editor flex flex-col h-full min-h-0"
      data-testid="markdown-editor"
      data-mode={mode}
    >
      {/* Toolbar */}
      <div
        className="flex items-center gap-3 flex-wrap px-3 py-2 border-b border-pir-border bg-pir-surface-1"
        role="toolbar"
        aria-label="Markdown editor toolbar"
      >
        <div className="flex items-center gap-0.5 rounded border border-pir-border bg-pir-surface-0 p-0.5">
          {MODES.map((m) => {
            const active = mode === m.id;
            return (
              <button
                key={m.id}
                type="button"
                onClick={() => setMode(m.id)}
                className={
                  active
                    ? "h-6 px-2 text-caption rounded font-mono uppercase tracking-wide bg-pir-accent/15 text-pir-accent border border-pir-accent/30"
                    : "h-6 px-2 text-caption rounded font-mono uppercase tracking-wide text-pir-text-tertiary hover:text-pir-text-primary border border-transparent"
                }
                style={{ fontFamily: "var(--pir-font-mono)" }}
                aria-pressed={active}
                aria-label={`Switch to ${m.label} mode`}
              >
                {m.label}
              </button>
            );
          })}
        </div>
        {!readOnly && mode === "wysiwyg" ? wysiwygActions : null}
      </div>

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-auto">
        {mode === "view" ? (
          <div data-md-export className="px-6 py-5 max-w-3xl mx-auto">
            <SafeMarkdown content={markdownState} />
          </div>
        ) : null}
        {mode === "raw" ? (
          <div className="px-6 py-5">
            <CodeEditor
              content={markdownState}
              language="markdown"
              readOnly={readOnly}
              onSave={handleRawSave}
              onChange={handleRawChange}
            />
          </div>
        ) : null}
        {mode === "wysiwyg" ? (
          <div className="px-6 py-5 max-w-3xl mx-auto">
            <EditorContent
              editor={editor}
              className="md-editor-content prose dark:prose-invert prose-sm max-w-none focus:outline-none prose-headings:text-pir-text-primary prose-p:text-pir-text-secondary prose-a:text-pir-accent prose-code:text-pir-accent/80 prose-pre:bg-pir-surface-1 prose-pre:border prose-pre:border-pir-border prose-li:text-pir-text-secondary prose-strong:text-pir-text-primary prose-blockquote:border-pir-border-strong prose-blockquote:text-pir-text-muted prose-hr:border-pir-border prose-th:text-pir-text-primary prose-th:border-pir-border prose-td:border-pir-border"
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
