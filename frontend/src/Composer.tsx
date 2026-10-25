import { ArrowUp, Check, ChevronUp, CornerDownLeft, Folder, Unlock } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { FileScope } from "./api";

const SCOPE_OPTIONS: {
  value: FileScope;
  label: string;
  detail: string;
}[] = [
  {
    value: "workspace",
    label: "仅工作文件夹",
    detail: "只能读写工作目录里的文件，命令也在此目录内执行；不可逆命令会被拒绝。",
  },
  {
    value: "machine",
    label: "完全访问权限",
    detail: "不设限：可读写整机任意文件，并执行任意系统命令。",
  },
];

/** 对话页的权限级别开关。做成"当前档位 + 点开选择"，避免用户看不见自己处在哪一档。 */
function ScopePicker({
  scope,
  fileRoot,
  onScope,
  disabled,
}: {
  scope: FileScope;
  fileRoot: string;
  onScope: (scope: FileScope) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const machine = scope === "machine";

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div className="scope-picker" ref={wrap}>
      <button
        type="button"
        className={`scope-button ${machine ? "machine" : ""}`}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={
          machine
            ? "完全访问权限：Orbit 可以读写整机任意文件、执行任意系统命令"
            : `仅工作文件夹：${fileRoot}`
        }
        onClick={() => setOpen((current) => !current)}
      >
        {machine ? <Unlock size={15} /> : <Folder size={15} />}
        <span>{machine ? "完全访问权限" : "仅工作文件夹"}</span>
        <ChevronUp size={13} className={open ? "flip" : ""} />
      </button>
      {open && (
        <div className="scope-menu" role="listbox" aria-label="文件访问级别">
          <p className="scope-menu-title">文件访问级别</p>
          {SCOPE_OPTIONS.map((option) => {
            const active = option.value === scope;
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={active}
                className={`scope-option ${active ? "active" : ""}`}
                onClick={() => {
                  setOpen(false);
                  if (!active) onScope(option.value);
                }}
              >
                <span className="scope-option-head">
                  {option.value === "machine" ? <Unlock size={14} /> : <Folder size={14} />}
                  <strong>{option.label}</strong>
                  {active && <Check size={14} className="scope-check" />}
                </span>
                <small>{option.detail}</small>
                {active && option.value === "workspace" && fileRoot && (
                  <code>{fileRoot}</code>
                )}
              </button>
            );
          })}
          <p className="scope-menu-note">
            改动立即生效，回复进行中不能切换。切换记录会写入任务事件。
          </p>
        </div>
      )}
    </div>
  );
}

export function Composer({
  draft,
  onDraft,
  onSend,
  disabled,
  scope,
  fileRoot,
  onScope,
}: {
  draft: string;
  onDraft: (value: string) => void;
  onSend: (event: FormEvent) => Promise<void>;
  disabled: boolean;
  scope: FileScope;
  fileRoot: string;
  onScope: (scope: FileScope) => void;
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
          <ScopePicker
            scope={scope}
            fileRoot={fileRoot}
            onScope={onScope}
            disabled={disabled}
          />
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
      <p className={`composer-hint ${scope === "machine" ? "warn" : ""}`}>
        {scope === "machine"
          ? "完全访问权限已开启：Orbit 可以读写整机任意文件、执行任意系统命令。"
          : "可以直接提问，也可以分享你正在看的内容。"}
      </p>
    </div>
  );
}
