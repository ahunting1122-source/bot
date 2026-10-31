/**
 * 桌面 UI 状态层 —— 把后端 4 条工具（`focus_pane` / `apply_layout` / `tip` / `tour`）
 * 写的状态**真的渲染出来**。
 *
 * ## 为什么这个文件存在（第 121 轮）
 *
 * 第 107 轮我把那 4 条工具判成「没有 UI 渲染就是空转，做了等于假覆盖」。
 * 用户明确要求「UI 状态也做进去，可以在 UI 上也模仿 Hermes」——
 * **前提变了，所以两头都做：后端存状态，这里渲染。**
 *
 * **判据：一条 UI 工具的价值 = 状态能存 + 用户能看到。缺一半就是假覆盖。**
 *
 * ## 四样东西各渲染成什么
 *
 * | 工具 | 渲染 |
 * |---|---|
 * | `focus_pane` | 底部布局条上对应面板高亮 + 一圈聚焦环 |
 * | `apply_layout` | 底部布局条按 `panes[].size` 显示真实占比 |
 * | `tip` | 锚到 `[data-ui="<target>"]` 的小气泡（找不到就居中显示） |
 * | `tour` | 变暗 + 高亮框 + 分步 popover + 上一步/下一步 |
 *
 * ## 轮询而不是推送
 *
 * 后端是「工具写文件、前端读文件」，**没有 websocket 通道**。
 * 2 秒轮询在这个体量下足够，而且**不用引入连接状态**。
 * `tips` 的过期由后端在读侧过滤，前端只管渲染。
 */

import { useCallback, useEffect, useRef, useState } from "react";

type Pane = { id: string; size: number };
type Tip = {
  id: string;
  target: string;
  text: string;
  placement: "top" | "bottom" | "left" | "right";
  expires_at: number;
};
type TourStep = { target: string; title: string; body: string };
type Tour = { steps: TourStep[]; index: number; active: boolean };
type UiStateData = {
  focused_pane?: string;
  layout?: { preset: string; panes: Pane[] };
  tips?: Tip[];
  tour?: Tour;
};

/** 面板 id → 给人看的名字。后端 `PANES` 是唯一事实源，这里只是显示名。 */
const PANE_LABELS: Record<string, string> = {
  chat: "对话",
  preview: "预览",
  files: "文件",
  skills: "技能",
  memories: "记忆",
  settings: "设置",
  kanban: "看板",
};

const POLL_MS = 2000;

async function fetchState(): Promise<UiStateData | null> {
  try {
    const response = await fetch("/api/ui/state", { credentials: "same-origin" });
    if (!response.ok) return null;
    return (await response.json()) as UiStateData;
  } catch {
    // **拉不到就保持上一帧** —— 闪一下空白比不显示更糟。
    return null;
  }
}

/** 目标元素的屏幕位置；找不到返回 null（调用方降级到居中）。 */
function rectOf(target: string): DOMRect | null {
  const el = document.querySelector(`[data-ui="${target}"]`);
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return rect;
}

function tipPosition(rect: DOMRect, placement: Tip["placement"]): React.CSSProperties {
  const gap = 10;
  switch (placement) {
    case "bottom":
      return { left: rect.left + rect.width / 2, top: rect.bottom + gap, transform: "translateX(-50%)" };
    case "left":
      return { left: rect.left - gap, top: rect.top + rect.height / 2, transform: "translate(-100%, -50%)" };
    case "right":
      return { left: rect.right + gap, top: rect.top + rect.height / 2, transform: "translateY(-50%)" };
    default:
      return { left: rect.left + rect.width / 2, top: rect.top - gap, transform: "translate(-50%, -100%)" };
  }
}

export function UiState() {
  const [state, setState] = useState<UiStateData>({});
  const [tick, setTick] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const pull = async () => {
      const next = await fetchState();
      if (alive.current && next) setState(next);
    };
    void pull();
    const timer = window.setInterval(() => {
      void pull();
      // 位置会随窗口变化，重渲染一次让气泡跟上。
      setTick((n) => n + 1);
    }, POLL_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, []);

  const advance = useCallback(async (direction: "next" | "prev") => {
    try {
      await fetch("/api/ui/tour/advance", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ direction }),
      });
      const next = await fetchState();
      if (next) setState(next);
    } catch {
      // 点不动就不动，不要弹一个用户看不懂的错误。
    }
  }, []);

  const panes = state.layout?.panes ?? [];
  const focused = state.focused_pane ?? "";
  const tips = state.tips ?? [];
  const tour = state.tour;

  return (
    <>
      {/* ── 布局条：按真实占比显示，聚焦的那个高亮 ── */}
      {panes.length > 0 && (
        <div
          aria-label="布局"
          data-testid="ui-layout-bar"
          style={{
            position: "fixed",
            left: 12,
            bottom: 12,
            zIndex: 40,
            display: "flex",
            gap: 4,
            padding: 6,
            borderRadius: 10,
            background: "rgba(20,20,24,0.82)",
            border: "1px solid rgba(255,255,255,0.12)",
            backdropFilter: "blur(6px)",
            maxWidth: "min(560px, 70vw)",
          }}
        >
          {panes.map((pane) => {
            const isFocused = pane.id === focused;
            return (
              <div
                key={pane.id}
                title={`${PANE_LABELS[pane.id] ?? pane.id} · ${pane.size}%`}
                data-focused={isFocused ? "true" : "false"}
                style={{
                  // 宽度按 size 占比 —— **布局条本身就是布局的缩略图**。
                  flexGrow: pane.size,
                  flexBasis: 0,
                  minWidth: 34,
                  textAlign: "center",
                  fontSize: 11,
                  lineHeight: "20px",
                  padding: "2px 6px",
                  borderRadius: 6,
                  color: isFocused ? "#0b0b0e" : "rgba(255,255,255,0.72)",
                  background: isFocused ? "#7dd3fc" : "rgba(255,255,255,0.06)",
                  fontWeight: isFocused ? 600 : 400,
                  transition: "background 160ms ease, color 160ms ease",
                }}
              >
                {PANE_LABELS[pane.id] ?? pane.id}
              </div>
            );
          })}
          <div style={{ fontSize: 10, color: "rgba(255,255,255,0.4)", alignSelf: "center", paddingLeft: 4 }}>
            {state.layout?.preset}
          </div>
        </div>
      )}

      {/* ── 气泡 ── */}
      {tips.map((tip) => {
        const rect = rectOf(tip.target);
        const anchor: React.CSSProperties = rect
          ? { position: "fixed", ...tipPosition(rect, tip.placement) }
          : { position: "fixed", left: "50%", top: "18%", transform: "translateX(-50%)" };
        return (
          <div
            key={tip.id}
            role="status"
            data-testid="ui-tip"
            style={{
              ...anchor,
              zIndex: 60,
              maxWidth: 260,
              padding: "8px 11px",
              borderRadius: 9,
              background: "#fde68a",
              color: "#20160a",
              fontSize: 12,
              lineHeight: 1.45,
              boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
            }}
          >
            {tip.text}
            {!rect && (
              <span style={{ display: "block", marginTop: 4, opacity: 0.55, fontSize: 10 }}>
                （界面上没有 data-ui=&quot;{tip.target}&quot;，居中显示）
              </span>
            )}
          </div>
        );
      })}

      {/* ── 导览：变暗 + 高亮 + popover ── */}
      {tour?.active && tour.steps.length > 0 && (() => {
        const step = tour.steps[Math.min(tour.index, tour.steps.length - 1)];
        const rect = rectOf(step.target);
        return (
          <>
            {/* 变暗层：**整块压暗，再在高亮框上用超大 box-shadow 挖洞** —— 
                比给每个元素加遮罩简单，而且不会漏。 */}
            <div
              data-testid="ui-tour-backdrop"
              style={{ position: "fixed", inset: 0, zIndex: 70, background: "rgba(0,0,0,0.55)" }}
            />
            {rect && (
              <div
                data-testid="ui-tour-highlight"
                style={{
                  position: "fixed",
                  left: rect.left - 6,
                  top: rect.top - 6,
                  width: rect.width + 12,
                  height: rect.height + 12,
                  zIndex: 71,
                  borderRadius: 10,
                  border: "2px solid #7dd3fc",
                  boxShadow: "0 0 0 9999px rgba(0,0,0,0.55)",
                  pointerEvents: "none",
                }}
              />
            )}
            <div
              data-testid="ui-tour-popover"
              style={{
                position: "fixed",
                zIndex: 72,
                left: rect ? Math.min(Math.max(rect.left, 16), window.innerWidth - 336) : "50%",
                top: rect ? Math.min(rect.bottom + 14, window.innerHeight - 190) : "30%",
                transform: rect ? undefined : "translateX(-50%)",
                width: 320,
                padding: 14,
                borderRadius: 12,
                background: "#18181c",
                color: "#f4f4f5",
                border: "1px solid rgba(125,211,252,0.5)",
                boxShadow: "0 16px 48px rgba(0,0,0,0.5)",
              }}
            >
              <div style={{ fontSize: 10, letterSpacing: 1, opacity: 0.55, marginBottom: 6 }}>
                导览 {tour.index + 1} / {tour.steps.length}
              </div>
              {step.title && <div style={{ fontWeight: 600, marginBottom: 6 }}>{step.title}</div>}
              {step.body && <div style={{ fontSize: 12, lineHeight: 1.55, opacity: 0.85 }}>{step.body}</div>}
              {!rect && (
                <div style={{ marginTop: 6, fontSize: 10, opacity: 0.5 }}>
                  （没有 data-ui=&quot;{step.target}&quot;，居中显示）
                </div>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
                <button
                  type="button"
                  onClick={() => void advance("prev")}
                  disabled={tour.index === 0}
                  style={{
                    fontSize: 12, padding: "4px 10px", borderRadius: 7,
                    border: "1px solid rgba(255,255,255,0.18)",
                    background: "transparent", color: "inherit",
                    opacity: tour.index === 0 ? 0.4 : 1, cursor: "pointer",
                  }}
                >
                  上一步
                </button>
                <button
                  type="button"
                  onClick={() => void advance("next")}
                  style={{
                    fontSize: 12, padding: "4px 12px", borderRadius: 7,
                    border: "none", background: "#7dd3fc", color: "#0b0b0e",
                    fontWeight: 600, cursor: "pointer",
                  }}
                >
                  {tour.index + 1 >= tour.steps.length ? "完成" : "下一步"}
                </button>
              </div>
            </div>
          </>
        );
      })()}
      {/* tick 只是为了让气泡在窗口尺寸变化后重新定位。 */}
      <span hidden data-ui-tick={tick} />
    </>
  );
}
