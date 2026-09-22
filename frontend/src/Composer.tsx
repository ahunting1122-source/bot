import { ArrowUp, CornerDownLeft, Folder } from "lucide-react";
import { useRef, type FormEvent } from "react";

export function Composer({
  draft,
  onDraft,
  onSend,
  disabled,
}: {
  draft: string;
  onDraft: (value: string) => void;
  onSend: (event: FormEvent) => Promise<void>;
  disabled: boolean;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  return (
    <div className="composer-wrap">
      <form className="composer" onSubmit={(event) => void onSend(event)}>
        <label className="sr-only" htmlFor="task-input">
          给 Orbit 发消息
        </label>
        <textarea
          ref={input}
          id="task-input"
          placeholder="想聊什么？从这里开始…"
          rows={3}
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              (event.ctrlKey || event.metaKey) &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              if (draft.trim() && !disabled) void onSend(event);
            }
          }}
        />
        <div className="composer-bottom">
          <span className="composer-source">
            <Folder size={15} />
            使用本地工作目录
          </span>
          <div className="composer-controls">
            <span className="keyboard-hint">
              Ctrl <CornerDownLeft size={12} />
            </span>
            <button
              className="send-button"
              type="submit"
              disabled={!draft.trim() || disabled}
              aria-label="发送消息"
            >
              <ArrowUp size={19} />
            </button>
          </div>
        </div>
      </form>
      <p className="composer-hint">可以直接提问，也可以分享你正在看的内容。</p>
    </div>
  );
}
