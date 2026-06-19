import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderDeepResearch, renderInlineMarkdown } from "../ActionViewModal";

describe("ActionViewModal Deep Research rendering", () => {
  it("renders spaced markdown bold markers as strong text", () => {
    render(<p>{renderInlineMarkdown("La tesi e' ** AI job panic **, non dati.")}</p>);

    const boldText = screen.getByText("AI job panic");
    expect(boldText.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it("renders structured deep research context without raw markdown markers", () => {
    const raw = JSON.stringify({
      context:
        "L'articolo contesta **allarmismo AI** e mostra che la narrativa regge poco. Se vuoi approfondire, trovi la critica alla lettura lineare del mercato.",
      signals: [],
      movers: [],
      reddit_hn: "Discussione centrata su **scetticismo HN**.",
      projects: [],
    });

    render(renderDeepResearch(raw));

    expect(screen.getByText("allarmismo AI").tagName).toBe("STRONG");
    expect(screen.getByText("scetticismo HN").tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });
});
