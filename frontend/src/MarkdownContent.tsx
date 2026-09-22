import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./markdown.css";

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href, title }) => (
            <a
              href={href}
              title={title}
              target="_blank"
              rel="noreferrer noopener"
            >
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div
              className="markdown-table"
              tabIndex={0}
              role="region"
              aria-label="表格，可横向滚动"
            >
              <table>{children}</table>
            </div>
          ),
          pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}
