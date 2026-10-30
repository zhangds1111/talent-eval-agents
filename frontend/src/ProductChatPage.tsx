import { useEffect, useRef, useState } from "react";
import { FeedbackButton, StepCaseFeedback } from "./EvaluationCenter";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Braces,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  FlaskConical,
  LayoutGrid,
  LoaderCircle,
  MessageSquarePlus,
  Send,
  Sparkles,
  UserSquare2,
  X,
} from "lucide-react";

import {
  defaultExpandedRunIds,
  formatDuration,
  getComposerPolicy,
  historyDetailToRunView,
  initialRunView,
  reduceRunEvent,
  shouldCloseStreamForEvent,
  type Citation,
  type RunEvent,
  type RunHistoryDetail,
  type RunView,
  type RunStep,
} from "./productRunState";
import "./product-chat.css";
import "./product-conversation.css";
import "./product-chat-interactions.css";
import "./product-run-history.css";

const EVENT_TYPES = [
  "run.started",
  "conversation.updated",
  "requirement.revised",
  "step.started",
  "step.progress",
  "step.completed",
  "hitl.required",
  "hitl.resolved",
  "report.completed",
  "run.failed",
  "run.cancelled",
  "run.completed",
];

type Props = {
  apiBase: string;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  onOpenDashboard: () => void;
  onOpenRecords: () => void;
  onOpenEvaluation?: () => void;
};

type ConversationSummary = {
  conversation_id: string;
  title: string;
  status: RunView["status"];
  updated_at: string;
  latest_run_id: string;
};

function runStatusLabel(status: RunView["status"]): string {
  if (status === "completed") return "已完成";
  if (status === "cancelled") return "已取消";
  if (status === "failed") return "已失败";
  if (status === "waiting_input") return "待确认";
  return "进行中";
}

async function requestJson(url: string, options?: RequestInit) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "请求失败");
  }
  return response.json();
}

export function MarkdownReport({ text, onCitation }: { text: string; onCitation: (ref: string) => void }) {
  return (
    <div className="reportMarkdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a({ href, children }) {
            const label = String(children);
            const citation = label.match(/^E\d+$/);
            return citation && href?.startsWith("/api/evidence/citations/") ? (
              <button type="button" className="citationLink" onClick={() => onCitation(label)}>
                [{label}]
              </button>
            ) : (
              <a href={href} target="_blank" rel="noreferrer">{children}</a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function StepStatusIcon({ step }: { step: RunStep }) {
  if (step.status === "completed") return <Check />;
  if (step.status === "running") return <LoaderCircle className="spin" />;
  return <CircleStop />;
}

function stepStatusLabel(step: RunStep): string {
  if (step.status === "completed") return "已完成";
  if (step.status === "paused") return "等待确认";
  if (step.status === "cancelled") return "已取消";
  if (step.status === "failed") return "已失败";
  return step.total ? `${step.completed || 0}/${step.total}` : "执行中";
}

export function ProductChatPage({ apiBase, sidebarCollapsed, onToggleSidebar, onOpenDashboard, onOpenRecords, onOpenEvaluation }: Props) {
  const [message, setMessage] = useState("我要为美国 Amazon 站选一批户外照明产品（露营灯、头灯），带电产品需要提供 FCC 和 UN38.3 检测报告，样品报价毛利率不低于 40%。请基于报价单、检测报告和用户评价给出候选商品的证据、风险与推荐结论");
  const [view, setView] = useState<RunView>(initialRunView);
  const [revision, setRevision] = useState("");
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<"idle" | "connected" | "reconnecting">("idle");
  const [activeCitation, setActiveCitation] = useState<Citation | null>(null);
  const [activeStepRun, setActiveStepRun] = useState("");
  const [activeStep, setActiveStep] = useState<RunStep | null>(null);
  const [expandedRunIds, setExpandedRunIds] = useState<Set<string>>(new Set());
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [historyStatus, setHistoryStatus] = useState("");
  const sourceRef = useRef<EventSource | null>(null);

  async function loadHistory() {
    const query = historyStatus ? `?status=${historyStatus}` : "";
    const items = await requestJson(`${apiBase}/conversations${query}`);
    setHistory(items as ConversationSummary[]);
  }

  useEffect(() => {
    void loadHistory();
  }, [apiBase, historyStatus]);

  function applyEvent(event: RunEvent) {
    setView((current) => reduceRunEvent(current, event));
    if (event.type === "hitl.required") {
      setRevision(typeof event.data.initial_value === "string" ? event.data.initial_value : "");
    }
    if (shouldCloseStreamForEvent(event.type)) {
      sourceRef.current?.close();
      setConnection("idle");
      setBusy(false);
      void loadHistory();
    } else if (event.type === "hitl.required") {
      setBusy(false);
    }
  }

  function connect(runId: string) {
    sourceRef.current?.close();
    setConnection("connected");
    const source = new EventSource(`${apiBase}/runs/${runId}/events`);
    let terminalEventReceived = false;
    sourceRef.current = source;
    EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (raw) => {
        const event = JSON.parse((raw as MessageEvent).data) as RunEvent;
        if (shouldCloseStreamForEvent(event.type)) {
          terminalEventReceived = true;
        }
        applyEvent(event);
        if (!shouldCloseStreamForEvent(event.type)) {
          setConnection("connected");
        }
      });
    });
    source.onerror = () => {
      if (!terminalEventReceived && source.readyState !== EventSource.CLOSED) {
        setConnection("reconnecting");
      }
    };
  }

  async function startRun() {
    if (!message.trim() || busy) return;
    const prompt = message.trim();
    setBusy(true);
    setMessage("");
    setActiveCitation(null);
    setActiveStep(null);
    const conversationId = view.conversationId;
    const conversationTurns = view.conversationTurns;
    setView({ ...initialRunView, conversationId, conversationTurns });
    try {
      const run = await requestJson(`${apiBase}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: prompt,
          ...(conversationId ? { conversation_id: conversationId } : {}),
        }),
      });
      setView({ ...initialRunView, runId: run.run_id, conversationId: run.conversation_id, turnNo: run.turn_no, conversationTurns, status: "running" });
      setExpandedRunIds(new Set([run.run_id]));
      connect(run.run_id);
    } catch (error) {
      setBusy(false);
      setMessage(prompt);
      setView({ ...initialRunView, conversationId, conversationTurns, status: "failed", error: (error as Error).message });
    }
  }

  async function resume(value: string) {
    if (!view.runId) return;
    setBusy(true);
    await requestJson(`${apiBase}/runs/${view.runId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  }

  async function cancel() {
    if (!view.runId) return;
    await requestJson(`${apiBase}/runs/${view.runId}`, { method: "DELETE" });
  }

  function reset() {
    sourceRef.current?.close();
    setView(initialRunView);
    setBusy(false);
    setConnection("idle");
    setActiveCitation(null);
    setActiveStep(null);
    setExpandedRunIds(new Set());
    setRevision("");
  }

  async function openHistory(runId: string) {
    sourceRef.current?.close();
    setConnection("idle");
    setBusy(false);
    setActiveCitation(null);
    setActiveStep(null);
    const detail = await requestJson(`${apiBase}/runs/history/${runId}`) as RunHistoryDetail;
    const restored = historyDetailToRunView(detail);
    setView(restored);
    setExpandedRunIds(new Set(defaultExpandedRunIds(restored.conversationTurns)));
  }

  function toggleRunAnswer(runId: string) {
    setExpandedRunIds((current) => {
      const next = new Set(current);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }

  const composerPolicy = getComposerPolicy(view.status);
  const composerLocked = busy || composerPolicy.disabled;

  return (
    <div className={`app chatApp${sidebarCollapsed ? " sidebarCollapsed" : ""}`}>
      <aside className="side">
        <div className="brand">
          <img src="/productos-favicon.png" alt="ProductOS" />
          <div><b>智鉴 ProductOS</b><small>多 AGENT 选品评估平台</small></div>
        </div>
        <nav>
          <button onClick={onOpenDashboard}><LayoutGrid /><span>工作台</span></button>
          <button onClick={onOpenRecords}><UserSquare2 /><span>商品档案</span></button>
          <button className="active"><Sparkles /><span>评估与推荐</span></button>
          <button onClick={onOpenEvaluation}><FlaskConical /><span>评测中心</span></button>
        </nav>
        <div className="env"><div><i /><b>测试环境</b></div><small>服务正常 · V1.0</small></div>
        <button type="button" className="sideToggle" onClick={onToggleSidebar} aria-label={sidebarCollapsed ? "展开导航栏" : "收起导航栏"}>
          {sidebarCollapsed ? <ChevronRight /> : <ChevronLeft />}
        </button>
      </aside>
      <main>
        <header>
          <div><span>评估与推荐</span><ChevronRight /><b>选品决策助手</b></div>
          <div className="currentUser"><b>Will</b><span>W</span></div>
        </header>
        <section className="chatWorkspace">
          <aside className="conversationRail">
            <button className="newConversation" onClick={reset}><MessageSquarePlus />新建会话</button>
            <label className="conversationFilter">状态筛选<select value={historyStatus} onChange={(event) => setHistoryStatus(event.target.value)}><option value="">全部</option><option value="running">进行中</option><option value="waiting_input">待确认</option><option value="completed">已完成</option><option value="failed">已失败</option><option value="cancelled">已取消</option></select></label>
            <div className="conversationGroup"><small>会话列表</small>{history.map((item) => <button key={item.conversation_id} className={view.conversationId === item.conversation_id ? "active" : ""} onClick={() => void openHistory(item.latest_run_id)} title={item.title}>{item.title}<span>{runStatusLabel(item.status)} · {new Date(item.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</span></button>)}{history.length === 0 && <p className="emptyHistory">暂无对话记录</p>}</div>
          </aside>
          <div className="chatPanel">
            <div className="chatHero"><div><span className="assistantMark"><Sparkles /></span><div><h1>选品决策助手</h1><p>输入选品需求（目标市场、品类、售价、毛利、认证偏好等），系统将实时展示理解、检索、评估与报告过程</p></div></div>
              <span className={`connection ${connection}`}><i />{connection === "connected" ? "实时连接" : connection === "reconnecting" ? "正在重连" : "就绪"}</span>
            </div>

            <div className="chatScroll">
              {view.status === "idle" && <div className="starterCards"><button onClick={() => setMessage("我要为美国 Amazon 站选一批户外照明产品（露营灯、头灯），带电产品需要提供 FCC 和 UN38.3 检测报告，样品报价毛利率不低于 40%。请基于报价单、检测报告和用户评价给出候选商品的证据、风险和推荐结论")}><b>按选品需求评估</b><span>目标市场、品类、认证与毛利要求</span></button><button onClick={() => setMessage("我想在欧洲站做儿童硅胶餐具，需要食品接触材料检测报告，样品报价毛利率不低于 35%。请基于现有材料给出候选商品的证据、风险和推荐结论")}><b>按条件检索</b><span>市场、品类、价格与口碑</span></button></div>}
              {view.conversationTurns.map((turn) => {
                const isCurrent = turn.runId === view.runId;
                const status = isCurrent ? view.status : turn.status;
                const steps = isCurrent ? view.steps : turn.steps;
                const report = isCurrent ? view.report : turn.report;
                const citations = isCurrent ? view.citations : turn.citations;
                const traceId = isCurrent ? view.traceId : turn.traceId;
                const error = isCurrent ? view.error : turn.error;
                const revisionAction = isCurrent ? view.revisionAction : turn.revisionAction;
                const revisionSummary = isCurrent ? view.revisionSummary : turn.revisionSummary;
                const expanded = expandedRunIds.has(turn.runId);
                return <article className="conversationTurn" key={turn.runId}>
                  <div className="turnBadge">第 {turn.turnNo} 轮</div>
                  <div className="userBubble">{turn.message}</div>
                  <section className={`turnAnswer${expanded ? " expanded" : " collapsed"}`}>
                    <button type="button" className="turnAnswerToggle" aria-expanded={expanded} onClick={() => toggleRunAnswer(turn.runId)}>
                      <span><Sparkles /><span><b>第 {turn.turnNo} 轮答案</b><small>{steps.length} 个执行步骤{report ? " · 已生成报告" : ""}</small></span></span>
                      <span><em>{runStatusLabel(status)}</em><ChevronDown /></span>
                    </button>
                    {expanded && <div className="turnAnswerBody">
                      {revisionSummary && <section className="resolvedRequest"><div><b>要求修订已识别</b><span>{revisionAction === "add" ? "增加要求" : revisionAction === "update" ? "修改要求" : "删除要求"}</span></div><p>{revisionSummary}</p></section>}
                      {steps.length > 0 && <section className="runTimeline"><div className="timelineHead"><div><Sparkles /><b>{status === "completed" ? "选品评估执行完成" : status === "cancelled" ? "选品评估已取消" : status === "waiting_input" ? "选品评估等待确认" : "正在执行选品评估"}</b></div><code>{turn.runId.slice(0, 8)}</code></div>{steps.map((step) => <button type="button" className={`timelineStep ${step.status}${activeStep === step ? " selected" : ""}`} key={step.stepId} onClick={() => {setActiveStep(step);setActiveStepRun(turn.runId);}}><span><StepStatusIcon step={step} /></span><div><b>{step.title}</b>{step.summary && <p>{step.summary}</p>}{step.total !== undefined && <div className="progressTrack"><i style={{ width: `${step.total ? ((step.completed || 0) / step.total) * 100 : 0}%` }} /></div>}</div><small><em>{stepStatusLabel(step)}</em><time>{formatDuration(step.durationMs)}</time></small></button>)}</section>}
                      {isCurrent && view.interrupt && <section className="hitlCard"><div className="hitlIcon"><Clock3 /></div><div><h3>{view.interrupt.title}</h3><p>{view.interrupt.description}</p>{view.interrupt.input_kind === "text" ? <div className="hitlRevision"><textarea value={revision} onChange={(event) => setRevision(event.target.value)} /><button disabled={!revision.trim()} onClick={() => void resume(revision)}>提交修订并继续</button></div> : <div className="hitlOptions">{view.interrupt.options.map((option) => <button key={option.value} onClick={() => void resume(option.value)}>{option.label}</button>)}</div>}</div></section>}
                      {report && <section className="reportCard"><div className="reportHead"><div><Check /><span><b>评估完成</b><small>结论均可回溯到商品原始材料</small></span></div><button className="traceButton">Run · {traceId.slice(0, 8)}</button></div><MarkdownReport text={report} onCitation={(ref) => setActiveCitation(citations.find((item) => item.ref === ref) || null)} /><FeedbackButton apiBase={apiBase} runId={turn.runId} /></section>}
                      {status === "cancelled" && <div className="runNotice">运行已取消，已产生的事件保留在当前页面</div>}
                      {error && <div className="runNotice error">{error}</div>}
                    </div>}
                  </section>
                </article>;
              })}
            </div>

            <div className={`composer${composerLocked ? " locked" : ""}`}><textarea value={composerLocked ? "" : message} disabled={composerLocked} onChange={(event) => setMessage(event.target.value)} placeholder={composerLocked ? "当前运行处理中" : view.conversationId ? "继续增加、修改或删除选品条件" : "输入选品需求（市场、品类、售价、毛利、认证偏好）"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !composerLocked) { event.preventDefault(); void startRun(); } }} /><div><span>{composerLocked ? "当前商品评估期间禁止编辑" : view.conversationId ? "沿用当前会话 · Enter 发送" : "Enter 发送 · Shift + Enter 换行"}</span>{composerPolicy.showCancel && view.runId ? <button className="cancelRun" onClick={() => void cancel()}><CircleStop />取消运行</button> : busy ? <button className="reconnectRun" disabled><LoaderCircle className="spin" />创建运行</button> : <button className="sendRun" disabled={!message.trim()} onClick={() => void startRun()}><Send />{view.conversationId ? "提交修订" : "开始评估"}</button>}</div></div>
          </div>
        </section>
      </main>

      {activeCitation && <div className="citationOverlay" onClick={() => setActiveCitation(null)}><aside className="citationDrawer" onClick={(event) => event.stopPropagation()}><button className="close" onClick={() => setActiveCitation(null)}><X /></button><small>{activeCitation.ref} · 证据原文</small><h2>{activeCitation.sku}</h2><p>{activeCitation.document}</p><blockquote>{activeCitation.quote}</blockquote><div className="citationMeta"><span>权限已校验</span><span>当前文档版本</span></div>{activeCitation.url && <code className="citationPath">{activeCitation.url}</code>}</aside></div>}
      {activeStep && <div className="stepOutputOverlay" onClick={() => setActiveStep(null)}><aside className="stepOutputDrawer" onClick={(event) => event.stopPropagation()}><button className="close" onClick={() => setActiveStep(null)}><X /></button><small><Braces />节点输出</small><h2>{activeStep.title}</h2><StepCaseFeedback apiBase={apiBase} runId={activeStepRun} stepId={activeStep.stepId} /><div className="stepOutputMeta"><span>{stepStatusLabel(activeStep)}</span><span>{formatDuration(activeStep.durationMs)}</span></div>{activeStep.output ? <pre>{JSON.stringify(activeStep.output, null, 2)}</pre> : <p className="emptyStepOutput">节点完成后显示受控的业务输出</p>}</aside></div>}
    </div>
  );
}
