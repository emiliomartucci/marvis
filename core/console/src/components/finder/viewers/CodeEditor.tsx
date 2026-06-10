"use client";

import { useCallback, useEffect, useRef, useState } from "react";

interface CodeEditorProps {
  content: string;
  language: string;
  readOnly: boolean;
  onSave: (content: string) => void;
  onChange: (value?: string) => void;
}

export default function CodeEditor({
  content,
  language,
  readOnly,
  onSave,
  onChange,
}: CodeEditorProps) {
  const [CodeMirror, setCodeMirror] = useState<any>(null);
  const [extensions, setExtensions] = useState<any[]>([]);
  const valueRef = useRef(content);

  // Load CodeMirror dynamically
  useEffect(() => {
    import("@uiw/react-codemirror").then((mod) => {
      setCodeMirror(() => mod.default);
    });
  }, []);

  // Load language extension
  useEffect(() => {
    const loadLang = async () => {
      const exts: any[] = [];
      try {
        switch (language) {
          case "python": {
            const mod = await import("@codemirror/lang-python");
            exts.push(mod.python());
            break;
          }
          case "javascript":
          case "typescript": {
            const mod = await import("@codemirror/lang-javascript");
            exts.push(
              mod.javascript({ typescript: language === "typescript", jsx: true })
            );
            break;
          }
          case "json": {
            const mod = await import("@codemirror/lang-json");
            exts.push(mod.json());
            break;
          }
          case "markdown": {
            const mod = await import("@codemirror/lang-markdown");
            exts.push(mod.markdown());
            break;
          }
          case "yaml": {
            const mod = await import("@codemirror/lang-yaml");
            exts.push(mod.yaml());
            break;
          }
          case "sql": {
            const mod = await import("@codemirror/lang-sql");
            exts.push(mod.sql());
            break;
          }
        }
      } catch {
        // Language not available
      }
      setExtensions(exts);
    };
    loadLang();
  }, [language]);

  // Keyboard shortcut for save
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        onSave(valueRef.current);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onSave]);

  const handleChange = useCallback(
    (value: string) => {
      valueRef.current = value;
      onChange(value);
    },
    [onChange]
  );

  if (!CodeMirror) {
    return <div className="flex-1 bg-pir-base animate-pulse" />;
  }

  return (
    <CodeMirror
      value={content}
      height="100%"
      theme="dark"
      extensions={extensions}
      readOnly={readOnly}
      onChange={handleChange}
      basicSetup={{
        lineNumbers: true,
        foldGutter: true,
        highlightActiveLine: true,
        highlightSelectionMatches: true,
        bracketMatching: true,
        closeBrackets: true,
        autocompletion: false,
      }}
      style={{ height: "100%", fontSize: "13px" }}
    />
  );
}
