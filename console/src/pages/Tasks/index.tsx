import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Empty,
  Input,
  message,
  Modal,
  Progress,
  Select,
  Spin,
  Tag,
} from "antd";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  CircleCheck,
  CircleStop,
  Clock3,
  Download,
  Eye,
  FileBox,
  FolderOpen,
  Gauge,
  ListChecks,
  MessageSquareText,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import {
  tasksApi,
  type LiteTask,
  type TaskArtifact,
  type TaskCapabilityDescriptor,
  type TaskPlanStep,
} from "../../api/modules/tasks";
import { Slot } from "../../plugins/registry/Slot";
import { useTaskData } from "./hooks/useTaskData";
import styles from "./index.module.less";

type DetailTab = "timeline" | "conversation" | "changes";

const ACTIVE_STATES: LiteTask["status"][] = [
  "planned",
  "running",
  "waiting_approval",
];
const DEFAULT_STRATEGY_ID = "qwenpaw.system.tasks.default-strategy";
const statusCopy: Record<LiteTask["status"], { label: string; color: string }> =
  {
    created: { label: "待启动", color: "default" },
    planned: { label: "已规划", color: "blue" },
    running: { label: "执行中", color: "processing" },
    waiting_approval: { label: "等待审批", color: "gold" },
    suspended: { label: "已暂停", color: "orange" },
    completed: { label: "已完成", color: "success" },
    failed: { label: "执行失败", color: "error" },
    cancelled: { label: "已取消", color: "default" },
  };

const eventCopy: Record<string, string> = {
  "task.created": "任务已创建",
  "task.planned": "执行计划已生成",
  "run.started": "Agent 开始执行",
  "conversation.user": "已接收任务消息",
  "runner.dispatched": "已进入 Console Agent Runtime",
  "runner.generating": "Agent 正在生成回复",
  "runner.responding": "Agent 开始返回内容",
  "artifact.produced": "已生成任务成果",
  "approval.requested": "请求人工审批",
  "approval.decided": "审批已处理",
  "run.resumed": "任务继续执行",
  "run.suspended": "已保存安全 Checkpoint",
  "run.completed": "任务执行完成",
  "run.failed": "任务执行失败",
  "run.cancelled": "任务已取消",
  "task.cancelled": "任务已取消",
};

const planTitleCopy: Record<string, string> = {
  "Execute task objective": "执行任务目标",
};

function relativeTime(value: string) {
  const elapsed = Math.max(0, Date.now() - new Date(value).getTime());
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return new Date(value).toLocaleDateString();
}

function stepState(
  task: LiteTask,
  step: TaskPlanStep,
  index: number,
  count: number,
) {
  void step;
  if (task.status === "completed") return "done";
  if (task.status === "failed" && index === 0) {
    return "failed";
  }
  if (task.status === "cancelled" && index === 0) return "cancelled";
  if (ACTIVE_STATES.includes(task.status) && index === 0) return "active";
  if (task.status === "suspended" && index === 0) return "paused";
  return count === 1 && task.status === "created" ? "ready" : "queued";
}

export default function TasksPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>("timeline");
  const [createOpen, setCreateOpen] = useState(false);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [projectDir, setProjectDir] = useState("");
  const [approvalLevel, setApprovalLevel] = useState("agent_profile");
  const [strategyId, setStrategyId] = useState(DEFAULT_STRATEGY_ID);
  const [strategies, setStrategies] = useState<TaskCapabilityDescriptor[]>([]);
  const [mutating, setMutating] = useState(false);
  const [previewArtifact, setPreviewArtifact] = useState<TaskArtifact | null>(
    null,
  );
  const [previewContent, setPreviewContent] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const {
    tasks,
    runs,
    plan,
    events,
    approvals,
    conversation,
    artifacts,
    evidence,
    capabilities,
    loading,
    error,
    refresh,
  } = useTaskData(selectedId);

  useEffect(() => {
    if (!selectedId && tasks.length > 0) setSelectedId(tasks[0].task_id);
  }, [selectedId, tasks]);

  useEffect(() => {
    if (!createOpen) return;
    const controller = new AbortController();
    void tasksApi
      .capabilities("strategy", controller.signal)
      .then((result) => setStrategies(result.items))
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") {
          return;
        }
        message.error("无法读取运行策略目录");
      });
    return () => controller.abort();
  }, [createOpen]);

  const selected = useMemo(
    () => tasks.find((task) => task.task_id === selectedId) ?? null,
    [selectedId, tasks],
  );
  const latestRun = runs[runs.length - 1] ?? null;
  const activeStrategy = capabilities.find(
    (capability) => capability.slot === "strategy",
  );

  useEffect(() => {
    if (!selected || !ACTIVE_STATES.includes(selected.status)) return;
    const timer = window.setInterval(() => void refresh(), 10_000);
    return () => window.clearInterval(timer);
  }, [refresh, selected]);

  const mutate = async (action: () => Promise<unknown>) => {
    setMutating(true);
    try {
      await action();
      await refresh();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setMutating(false);
    }
  };

  const createAndStart = async () => {
    const normalized = objective.trim();
    if (!normalized) {
      message.warning("请输入任务目标");
      return;
    }
    setMutating(true);
    try {
      const created = await tasksApi.create({
        objective: normalized,
        acceptance_criteria: criteria
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
        ...(projectDir.trim() ? { project_dir: projectDir.trim() } : {}),
        strategy_id: strategyId,
        ...(approvalLevel !== "agent_profile"
          ? {
              approval_level: approvalLevel as
                | "strict"
                | "smart"
                | "auto"
                | "off",
            }
          : {}),
      });
      setSelectedId(created.task_id);
      setCreateOpen(false);
      setObjective("");
      setCriteria("");
      setProjectDir("");
      setApprovalLevel("agent_profile");
      setStrategyId(DEFAULT_STRATEGY_ID);
      await tasksApi.start(created.task_id);
      message.success("任务已进入 Console Agent Runtime");
      await refresh();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "任务启动失败");
    } finally {
      setMutating(false);
    }
  };

  const openArtifactPreview = async (artifact: TaskArtifact) => {
    if (!selected) return;
    setPreviewArtifact(artifact);
    setPreviewContent("");
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const content = await tasksApi.artifactContent(
        selected.task_id,
        artifact.artifact_id,
      );
      setPreviewContent(content);
    } catch (reason) {
      setPreviewError(
        reason instanceof Error ? reason.message : "无法读取成果内容",
      );
    } finally {
      setPreviewLoading(false);
    }
  };

  const renderPlan = () => {
    if (!plan || plan.steps.length === 0) {
      return <Empty description="任务启动后将生成执行计划" />;
    }
    return (
      <>
        <div className={styles.planHeading}>
          <div>
            <strong>执行计划</strong>
            <span>目标拆解与当前执行位置</span>
          </div>
          <span>
            Revision {plan.revision} · {plan.steps.length} 个步骤
          </span>
        </div>
        <div className={styles.planTimeline}>
          {plan.steps.map((step, index) => {
            const state = stepState(selected!, step, index, plan.steps.length);
            return (
              <div
                className={`${styles.planStep} ${styles[`step-${state}`]}`}
                key={step.step_id}
              >
                <div className={styles.stepMarker}>
                  {state === "done" ? (
                    <Check size={15} />
                  ) : state === "failed" ? (
                    <X size={15} />
                  ) : state === "cancelled" ? (
                    <CircleStop size={15} />
                  ) : (
                    <span>{index + 1}</span>
                  )}
                </div>
                <div className={styles.stepBody}>
                  <strong>{planTitleCopy[step.title] ?? step.title}</strong>
                  <span>{step.objective}</span>
                </div>
                <span className={styles.stepStatus}>
                  {state === "done"
                    ? "完成"
                    : state === "active"
                    ? "进行中"
                    : state === "failed"
                    ? "失败"
                    : state === "cancelled"
                    ? "已取消"
                    : state === "paused"
                    ? "已暂停"
                    : "待执行"}
                </span>
              </div>
            );
          })}
        </div>
      </>
    );
  };

  const renderArtifacts = () => (
    <div className={styles.outcomeList}>
      {artifacts.length === 0 && evidence.length === 0 ? (
        <Empty description="Agent 尚未产生成果或证据" />
      ) : (
        <>
          {artifacts.map((artifact) => (
            <div className={styles.outcomeItem} key={artifact.artifact_id}>
              <span className={styles.fileBadge}>
                {artifact.kind.slice(0, 3).toUpperCase()}
              </span>
              <div>
                <strong>
                  {String(artifact.metadata?.name ?? artifact.kind)}
                </strong>
                <span>
                  {artifact.media_type} · {artifact.size_bytes} bytes
                </span>
              </div>
              <div className={styles.artifactActions}>
                <button
                  type="button"
                  disabled={!artifact.preview.available}
                  title={
                    artifact.preview.reason === "too_large"
                      ? "成果超过内联预览大小限制"
                      : artifact.preview.reason === "unsupported"
                      ? "当前没有安全 Renderer 支持此类型"
                      : `由 ${artifact.preview.renderer_id} 渲染`
                  }
                  onClick={() => void openArtifactPreview(artifact)}
                >
                  <Eye size={14} /> 预览
                </button>
                {selected && (
                  <a
                    download
                    href={tasksApi.artifactDownloadUrl(
                      selected.task_id,
                      artifact.artifact_id,
                    )}
                  >
                    <Download size={14} /> 下载
                  </a>
                )}
              </div>
              <Slot
                name="ui.artifact.preview"
                kind="fill"
                context={{ task: selected, artifact, evidence }}
              />
            </div>
          ))}
          {evidence.map((item) => (
            <div className={styles.outcomeItem} key={item.evidence_id}>
              <span className={styles.fileBadge}>
                <ShieldCheck size={15} />
              </span>
              <div>
                <strong>{item.claim}</strong>
                <span>证据来源 · {item.producer}</span>
              </div>
              <CircleCheck size={17} />
            </div>
          ))}
        </>
      )}
    </div>
  );

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div>
          <span className={styles.eyebrow}>Agent OS · Lite</span>
          <h1>任务工作台</h1>
          <p>目标、计划、执行、证据和成果归属于同一个任务。</p>
        </div>
        <div className={styles.headerActions}>
          <Slot
            name="ui.task.toolbar"
            kind="fill"
            context={{ task: selected, tasks }}
          />
          <Button icon={<RefreshCw size={15} />} onClick={() => void refresh()}>
            刷新
          </Button>
          <Button
            type="primary"
            className={styles.primaryAction}
            icon={<Plus size={16} />}
            onClick={() => setCreateOpen(true)}
          >
            新建任务
          </Button>
        </div>
      </header>

      {error && <div className={styles.error}>{error}</div>}
      <Spin spinning={loading}>
        <section className={styles.workbench}>
          <aside className={styles.taskRail} aria-label="任务列表">
            <div className={styles.workspaceLabel}>Workspace</div>
            <div className={styles.railTitle}>
              <strong>QwenPaw Tasks</strong>
              <span>{tasks.length} 项任务</span>
            </div>
            {tasks.length === 0 ? (
              <Empty description="还没有任务" />
            ) : (
              tasks.map((task) => (
                <button
                  type="button"
                  key={task.task_id}
                  className={`${styles.taskItem} ${
                    selectedId === task.task_id ? styles.taskItemActive : ""
                  }`}
                  onClick={() => setSelectedId(task.task_id)}
                >
                  <span
                    className={`${styles.statusDot} ${
                      styles[`status-${task.status}`]
                    }`}
                  />
                  <span className={styles.taskItemContent}>
                    <strong>{task.objective}</strong>
                    <small>
                      {statusCopy[task.status].label} ·{" "}
                      {relativeTime(task.updated_at)}
                    </small>
                  </span>
                  <ChevronRight size={15} />
                </button>
              ))
            )}
          </aside>

          <div className={styles.detail}>
            {!selected ? (
              <div className={styles.emptyDetail}>
                <Sparkles size={28} />
                <h2>创建第一个可执行任务</h2>
                <p>QwenPaw 会在 Console Agent Runtime 中执行并持续记录。</p>
                <Button
                  type="primary"
                  className={styles.primaryAction}
                  onClick={() => setCreateOpen(true)}
                >
                  新建任务
                </Button>
              </div>
            ) : (
              <>
                <section className={styles.taskHero}>
                  <div className={styles.heroTopline}>
                    <Tag color={statusCopy[selected.status].color}>
                      {statusCopy[selected.status].label}
                    </Tag>
                    <span>{relativeTime(selected.updated_at)}</span>
                  </div>
                  <div className={styles.detailHeader}>
                    <div>
                      <h2>{selected.objective}</h2>
                      <p>
                        {selected.status === "running"
                          ? "Agent 正在执行任务并持续写入事件账本"
                          : "任务的计划、运行、审批与成果均可在此追溯"}
                      </p>
                    </div>
                    <div className={styles.actions}>
                      {selected.status === "created" && (
                        <Button
                          type="primary"
                          className={styles.primaryAction}
                          loading={mutating}
                          icon={<Play size={15} />}
                          onClick={() =>
                            void mutate(() => tasksApi.start(selected.task_id))
                          }
                        >
                          开始任务
                        </Button>
                      )}
                      {["suspended", "failed"].includes(selected.status) && (
                        <Button
                          type="primary"
                          className={styles.primaryAction}
                          loading={mutating}
                          icon={<RotateCcw size={15} />}
                          onClick={() =>
                            void mutate(() => tasksApi.resume(selected.task_id))
                          }
                        >
                          继续任务
                        </Button>
                      )}
                      {!["completed", "failed", "cancelled"].includes(
                        selected.status,
                      ) && (
                        <Button
                          danger
                          loading={mutating}
                          icon={<CircleStop size={15} />}
                          onClick={() =>
                            void mutate(() => tasksApi.cancel(selected.task_id))
                          }
                        >
                          取消
                        </Button>
                      )}
                      <Slot
                        name="ui.task.inspector"
                        kind="fill"
                        context={{ task: selected, run: latestRun, plan }}
                      />
                    </div>
                  </div>
                  <div className={styles.agentLine}>
                    {selected.status === "failed" ? (
                      <AlertTriangle size={16} />
                    ) : selected.status === "completed" ? (
                      <CircleCheck size={16} />
                    ) : (
                      <Sparkles size={16} />
                    )}
                    <span>
                      {latestRun?.runner_id ?? "Agent 尚未启动"}
                      {selected.status === "running" ? " · 正在工作" : ""}
                    </span>
                  </div>
                </section>

                {approvals.length > 0 && (
                  <section className={styles.approvalPanel}>
                    <div className={styles.approvalHeading}>
                      <ShieldCheck size={19} />
                      <div>
                        <strong>任务需要你的确认</strong>
                        <span>
                          {approvals.length} 个受保护动作正在等待审批。
                        </span>
                      </div>
                    </div>
                    <div className={styles.approvalList}>
                      {approvals.map((approval) => (
                        <article
                          className={styles.approvalItem}
                          key={approval.approval_id}
                        >
                          <div className={styles.approvalDetails}>
                            <div className={styles.approvalMeta}>
                              <Tag color="gold">{approval.risk}</Tag>
                              <Tag>{approval.source}</Tag>
                              <span>{approval.policy}</span>
                            </div>
                            <strong>
                              {approval.display?.title ?? approval.action}
                            </strong>
                            <span>
                              {approval.display?.summary ??
                                "此动作需要用户确认后才能继续。"}
                            </span>
                            {Object.keys(approval.redacted_arguments).length >
                              0 && (
                              <code className={styles.approvalArguments}>
                                {JSON.stringify(
                                  approval.redacted_arguments,
                                  null,
                                  2,
                                )}
                              </code>
                            )}
                          </div>
                          <div className={styles.actions}>
                            <Button
                              icon={<X size={14} />}
                              onClick={() =>
                                void mutate(() =>
                                  tasksApi.decide(
                                    selected.task_id,
                                    approval.approval_id,
                                    "denied",
                                  ),
                                )
                              }
                            >
                              拒绝
                            </Button>
                            <Button
                              type="primary"
                              className={styles.primaryAction}
                              icon={<Check size={14} />}
                              onClick={() =>
                                void mutate(() =>
                                  tasksApi.decide(
                                    selected.task_id,
                                    approval.approval_id,
                                    "approved",
                                  ),
                                )
                              }
                            >
                              批准并继续
                            </Button>
                          </div>
                        </article>
                      ))}
                    </div>
                  </section>
                )}

                <nav className={styles.tabs} aria-label="任务详情视图">
                  <button
                    className={activeTab === "timeline" ? styles.tabActive : ""}
                    onClick={() => setActiveTab("timeline")}
                    type="button"
                  >
                    <ListChecks size={15} /> 执行时间线
                  </button>
                  <button
                    className={
                      activeTab === "conversation" ? styles.tabActive : ""
                    }
                    onClick={() => setActiveTab("conversation")}
                    type="button"
                  >
                    <MessageSquareText size={15} /> 对话
                  </button>
                  <button
                    className={activeTab === "changes" ? styles.tabActive : ""}
                    onClick={() => setActiveTab("changes")}
                    type="button"
                  >
                    <FileBox size={15} /> 成果变更
                  </button>
                </nav>

                <section className={styles.primaryPanel}>
                  {activeTab === "timeline" && (
                    <>
                      {renderPlan()}
                      <div className={styles.activityTitle}>运行事件</div>
                      <div className={styles.eventGrid}>
                        {events
                          .filter(
                            (event) =>
                              event.event_type !==
                              "conversation.assistant.delta",
                          )
                          .map((event) => (
                            <div className={styles.event} key={event.event_id}>
                              <span className={styles.eventDot} />
                              <div>
                                <strong>
                                  {eventCopy[event.event_type] ??
                                    event.event_type}
                                </strong>
                                <span>
                                  #{event.sequence} ·{" "}
                                  {new Date(event.occurred_at).toLocaleString()}
                                </span>
                              </div>
                            </div>
                          ))}
                      </div>
                    </>
                  )}
                  {activeTab === "conversation" && (
                    <div className={styles.conversation}>
                      {conversation.length === 0 ? (
                        <div className={styles.conversationEmpty}>
                          <MessageSquareText size={24} />
                          <strong>此任务还没有可见消息</strong>
                          <span>
                            旧任务可能创建于消息投影启用之前；新任务会在这里实时显示用户输入与
                            Agent 回复。
                          </span>
                        </div>
                      ) : (
                        conversation.map((item, index) => (
                          <div
                            className={`${styles.messageRow} ${
                              item.role === "user"
                                ? styles.messageUser
                                : styles.messageAssistant
                            }`}
                            key={`${item.created_at}-${index}`}
                          >
                            <div className={styles.messageMeta}>
                              <strong>
                                {item.role === "user" ? "你" : "QwenPaw"}
                              </strong>
                              <span>
                                {new Date(item.created_at).toLocaleTimeString()}
                              </span>
                            </div>
                            <div className={styles.messageBubble}>
                              {item.text}
                            </div>
                          </div>
                        ))
                      )}
                      {selected.status === "running" && (
                        <div className={styles.conversationStatus}>
                          <Sparkles size={15} /> Agent 正在继续处理
                        </div>
                      )}
                    </div>
                  )}
                  {activeTab === "changes" && renderArtifacts()}
                  <Slot
                    name="ui.task.tab"
                    kind="fill"
                    context={{
                      task: selected,
                      run: latestRun,
                      plan,
                      events,
                      artifacts,
                      evidence,
                    }}
                  />
                </section>

                <section className={styles.summaryGrid}>
                  <div className={styles.summaryCard}>
                    <h3>任务状态</h3>
                    <dl>
                      <div>
                        <dt>
                          <Gauge size={15} />
                          执行引擎
                        </dt>
                        <dd>{latestRun?.runner_id ?? "未分配"}</dd>
                      </div>
                      <div>
                        <dt>
                          <Sparkles size={15} />
                          运行策略
                        </dt>
                        <dd>{activeStrategy?.capability_id ?? "未分配"}</dd>
                      </div>
                      <div>
                        <dt>
                          <FolderOpen size={15} />
                          工作目录
                        </dt>
                        <dd>
                          {String(
                            selected.metadata?.workspace_dir ??
                              "当前 Workspace",
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt>
                          <ShieldCheck size={15} />
                          权限
                        </dt>
                        <dd>Tool Guard / Sandbox</dd>
                      </div>
                      <div>
                        <dt>
                          <Clock3 size={15} />
                          Checkpoint
                        </dt>
                        <dd>
                          {latestRun?.checkpoint_id
                            ? latestRun.checkpoint_id.slice(0, 8)
                            : "尚未生成"}
                        </dd>
                      </div>
                    </dl>
                  </div>
                  <div className={styles.summaryCard}>
                    <div className={styles.cardHeading}>
                      <h3>成果</h3>
                      <span>{artifacts.length + evidence.length} 项</span>
                    </div>
                    {renderArtifacts()}
                  </div>
                  <div className={styles.summaryCard}>
                    <div className={styles.cardHeading}>
                      <h3>证据覆盖</h3>
                      <span>{evidence.length} 条</span>
                    </div>
                    <Progress
                      percent={
                        artifacts.length === 0
                          ? 0
                          : Math.min(
                              100,
                              Math.round(
                                (evidence.length / artifacts.length) * 100,
                              ),
                            )
                      }
                      showInfo={false}
                      strokeColor="var(--app-accent)"
                    />
                    <div className={styles.coverageMeta}>
                      <span>Artifacts</span>
                      <strong>{artifacts.length}</strong>
                      <span>Evidence</span>
                      <strong>{evidence.length}</strong>
                    </div>
                  </div>
                </section>
              </>
            )}
          </div>
        </section>
      </Spin>

      <Modal
        rootClassName={styles.artifactModal}
        title={String(
          previewArtifact?.metadata?.name ??
            previewArtifact?.kind ??
            "成果预览",
        )}
        open={previewArtifact !== null}
        footer={null}
        width={760}
        onCancel={() => setPreviewArtifact(null)}
      >
        <Spin spinning={previewLoading}>
          {previewError ? (
            <div className={styles.error}>{previewError}</div>
          ) : (
            <pre className={styles.artifactPreview}>{previewContent}</pre>
          )}
        </Spin>
      </Modal>

      <Modal
        rootClassName={styles.taskModal}
        title="新建可执行任务"
        open={createOpen}
        okText="创建并开始"
        okButtonProps={{ className: styles.primaryAction }}
        cancelText="取消"
        confirmLoading={mutating}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void createAndStart()}
      >
        <div className={styles.createForm}>
          <label htmlFor="task-objective">任务目标</label>
          <Input.TextArea
            id="task-objective"
            autoSize={{ minRows: 3, maxRows: 7 }}
            placeholder="例如：检查当前项目结构，并给出下一阶段重构建议"
            value={objective}
            onChange={(event) => setObjective(event.target.value)}
          />
          <label htmlFor="task-criteria">验收标准（每行一项，可选）</label>
          <Input.TextArea
            id="task-criteria"
            autoSize={{ minRows: 2, maxRows: 5 }}
            placeholder={"输出结构化结论\n列出风险与下一步"}
            value={criteria}
            onChange={(event) => setCriteria(event.target.value)}
          />
          <label htmlFor="task-project-dir">工作目录（可选）</label>
          <Input
            id="task-project-dir"
            placeholder="留空使用 Agent Workspace"
            value={projectDir}
            onChange={(event) => setProjectDir(event.target.value)}
          />
          <label htmlFor="task-strategy">运行策略</label>
          <Select
            id="task-strategy"
            value={strategyId}
            onChange={setStrategyId}
            options={(strategies.length > 0
              ? strategies
              : [
                  {
                    capability_id: DEFAULT_STRATEGY_ID,
                    slot: "strategy",
                    provider_id: "qwenpaw.system",
                    provider_kind: "system",
                    version: "1",
                    metadata: { label: "Default" },
                  },
                ]
            ).map((strategy) => ({
              value: strategy.capability_id,
              label: `${String(
                strategy.metadata.label ?? strategy.capability_id,
              )} · ${strategy.provider_id ?? "qwenpaw.system"}`,
            }))}
          />
          <label htmlFor="task-approval-level">审批策略</label>
          <Select
            id="task-approval-level"
            value={approvalLevel}
            onChange={setApprovalLevel}
            options={[
              { value: "agent_profile", label: "继承 Agent 配置" },
              { value: "strict", label: "严格 · 每个工具均需审批" },
              { value: "smart", label: "智能 · 中高风险需审批" },
              { value: "auto", label: "自动 · 仅受保护工具" },
              { value: "off", label: "关闭 · 不请求审批" },
            ]}
          />
        </div>
      </Modal>
    </main>
  );
}
