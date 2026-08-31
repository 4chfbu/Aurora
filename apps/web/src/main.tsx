import { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { AlertTriangle, Bot, Boxes, BrainCircuit, Check, ChevronRight, CircleDot, Download, ExternalLink, FileSearch, Flag, FolderInput, GitBranch, History, Network, PauseCircle, Play, Plus, RefreshCw, RotateCcw, ShieldCheck, Sparkles, TerminalSquare, TimerReset, Trash2, Wrench, X } from 'lucide-react';
import '@xyflow/react/dist/style.css';
import './styles.css';
import './enhancements.css';

const API_BASE = import.meta.env.VITE_API_BASE || '';
const IMPORT_PROGRESS_STEPS = ['创建导入任务', '连接题目列表', '识别平台', '采集动态数据', '归集题目', '验证与检查附件', '暂存附件'];
const IMPORT_PROGRESS_BY_PHASE: Record<string, { step: number; percent: number }> = {
  QUEUED: { step: 0, percent: 6 },
  FETCHING: { step: 1, percent: 16 },
  DETECTING: { step: 2, percent: 28 },
  BROWSING: { step: 3, percent: 42 },
  CATALOGING: { step: 4, percent: 58 },
  VALIDATING: { step: 5, percent: 72 },
  DETAILS: { step: 5, percent: 82 },
  STAGING: { step: 6, percent: 92 },
};

type Project = { id: string; name: string; goal: string; status: string; target_verification_status?: string; target_verification_reason?: string; target_url?: string };
type Intent = { id: string; objective: string; status: string; priority: number; parent_intent_id?: string };
type EvidenceItem = { description: string; artifact_refs: string[] };
type EvidenceDraft = EvidenceItem;
type Fact = { id: string; statement: string; confidence: number; evidence_refs: string[]; evidence_items: EvidenceItem[]; source_attempt_id?: string; category?: string; status: string; created_at: string };
type Attempt = { id: string; intent_id: string; worker_id: string; parent_attempt_id?: string; status: string; result_summary?: string; artifact_refs: string[]; codex_thread_id?: string; last_event_at?: string; resume_count: number; blackboard_version: number };
type Artifact = { id: string; source_attempt_id?: string; type: string; summary?: string; size: number; created_at: string };
type Finding = { id: string; severity: string; title: string; reproduction?: string; evidence_refs: string[] };
type FlagCandidate = { id: string; value: string; status: string; provenance_kind: string; artifact_refs: string[]; verification_artifact_ref?: string; submission_count: number; rejection_reason?: string };
type Worker = { id: string; intent_id: string; parent_worker_id?: string; execution_kind?: string; status: string; capability_set: string[]; agent_profile_id?: string };
type RuntimePolicy = { multi_agent_exploration_enabled: boolean; max_parallel_explorers: number; max_reason_intents: number; max_pending_intents: number };
type CoordinationState = { graph_version: number; last_reasoned_version: number; reason_lease_owner?: string };
type AttemptCheckpoint = { id: string; intent_id: string; worker_id?: string; attempt_id: string; parent_checkpoint_id?: string; status: string; summary: string; conclusions: string[]; hypotheses: string[]; failed_routes: string[]; next_steps: string[]; fact_refs: string[]; artifact_refs: string[]; generated_intent_ids: string[]; source: string; created_at: string };
type ContextSnapshot = { id: string; total_chars: number; estimated_tokens: number };
type LLMTrace = { id: string; model: string; estimated_input_tokens: number; estimated_output_tokens: number; decision_summary: Record<string, unknown> };
type ToolTrace = { id: string; tool_name: string; policy_decision: string; exit_code?: number; summary?: string; artifact_refs: string[] };
type WorkerEvent = { id: string; event_type: string; worker_id?: string; attempt_id?: string; payload_json: Record<string, unknown>; created_at: string };
type DiscoveredTarget = { id: string; url: string; host: string; status: string; source: string; confidence: number; probe_json?: { success?: boolean; code?: string; summary?: string; diagnostics?: Record<string, unknown> }; source_artifact_id?: string; created_at: string };
type ChallengeGroup = { id: string; name: string; status: string; current_item_id?: string; max_concurrent?: number; limits?: Record<string, unknown>; deadline_at?: string; flag_prefixes?: string[]; created_at: string };
type ChallengeGroupItem = { id: string; project_id: string; position: number; status: string; fused_status?: string; phase?: number; hint_taken?: boolean; submission_status?: string; stop_reason?: string };
type ChallengeGroupDetail = { group: ChallengeGroup; items: ChallengeGroupItem[]; projects: Record<string, Project>; candidate_flags: Record<string, string>; flag_candidates?: FlagCandidate[]; background?: Record<string, unknown> };
type Hint = { id: string; content: string; source: string; consumed: boolean; created_at: string };
type Blackboard = { project: Project; facts: Fact[]; intents: Intent[]; attempts: Attempt[]; artifacts: Artifact[]; findings: Finding[]; flag_candidates: FlagCandidate[]; workers: Worker[]; checkpoints: AttemptCheckpoint[]; runtime_policy?: RuntimePolicy; coordination?: CoordinationState };
type RuntimeLogs = { containers: Array<{ id: string; info: string; stdout: string; stderr: string }>; error?: string };
type ImportDiagnostic = { code: string; message?: string; challenge_id?: string; step?: number };
type ImportBatch = { id: string; source_url: string; status: string; title?: string; summary?: string; error?: string; auth_method?: string; login_domain?: string; auth_message?: string; platform?: string; extraction_strategy?: string; pages_scanned?: number; diagnostics_json: ImportDiagnostic[] };
type ExternalAttachment = { url?: string; filename?: string; status: string; reason?: string };
type ImportCandidate = { id: string; title: string; description?: string; challenge_url: string; challenge_type?: string; confidence: number; staged_attachments_json: Array<{ filename: string; size: number }>; external_attachments_json: ExternalAttachment[]; source_metadata_json?: Record<string, unknown>; project_id?: string };
type ImportResult = { batch: ImportBatch; candidates: ImportCandidate[] };
type ImportProgress = 'idle' | 'scanning' | 'ready' | 'needs_session' | 'failed';
type NetworkProxyConfig = { mode: 'direct' | 'system' | 'custom'; proxy_url?: string | null; no_proxy: string };
type ConcurrencyConfig = { max_agents: number; min: number; max: number; source: 'environment' | 'runtime' | 'default'; env_value?: number | null };
type TSecBenchConfig = { base_url: string; token_configured: boolean; token_source: 'environment' | 'runtime' | 'none'; timeout_seconds: number; max_concurrent: number; vpn_required: boolean };
type TSecBenchTestResult = { api: { status: string; challenge_count: number }; vpn: { status: 'reachable' | 'unreachable' | 'unverified'; address?: string | null; message: string }; progress: { completed: number; correct_flags: number; total_flags: number } };
type SlabMatchConfig = { base_url: string; access_key_configured: boolean; access_key_source: 'environment' | 'runtime' | 'none'; timeout_seconds: number; max_concurrent: number; vpn_required: boolean };
type SlabMatchTestResult = { api: { status: string; challenge_count: number; match_info?: { note?: string; rule?: string } }; endpoint: { status: 'reachable' | 'unreachable' | 'unverified'; address?: string | null; message: string }; progress: { stagePoint?: number; stageRank?: number } };
type SlabMatchImportResult = { group: ChallengeGroup; challenge_count: number; attachment_first_count: number; max_environments: number; projects: Array<{ project_id: string; exercise_id: number; name: string; requires_environment: boolean; artifact_count: number; attachment_errors: number }> };
type SlabMatchAttachmentRepairResult = { group_id: string; checked: number; repaired_projects: number; artifact_count: number; failures: Array<{ exercise_id: number; reason?: string }> };
type OpenVPNConfig = { configured: boolean; locked: boolean; connected: boolean; desired_connected: boolean; state: 'unconfigured' | 'locked' | 'unlocked' | 'connected' | 'error'; routes: string[]; credentials_configured: boolean; last_error?: string | null; updated_at?: string | null };
type ProgressNodeData = { kind: 'intent' | 'worker' | 'attempt' | 'activity' | 'checkpoint' | 'hypothesis' | 'fact' | 'artifact' | 'finding'; label: string; meta: string; status: string; entityId: string };
type DrawerTab = 'inspect' | 'controls' | 'evidence';

const toolOptions = ['sandbox.exec', 'http.request', 'browser.interact', 'network.scan', 'web.enumerate', 'binary.inspect', 'forensic.inspect'];
const activeStatuses = new Set(['PENDING', 'CLAIMED', 'RUNNING', 'PARTIAL']);

function App() {
  const eventRefreshTimer = useRef<number | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [groups, setGroups] = useState<ChallengeGroup[]>([]);
  const [selectedGroupId, setSelectedGroupId] = useState('');
  const [groupDetail, setGroupDetail] = useState<ChallengeGroupDetail | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [blackboard, setBlackboard] = useState<Blackboard | null>(null);
  const [contexts, setContexts] = useState<ContextSnapshot[]>([]);
  const [traces, setTraces] = useState<LLMTrace[]>([]);
  const [toolTraces, setToolTraces] = useState<ToolTrace[]>([]);
  const [events, setEvents] = useState<WorkerEvent[]>([]);
  const [warnings, setWarnings] = useState<WorkerEvent[]>([]);
  const [targets, setTargets] = useState<DiscoveredTarget[]>([]);
  const [hints, setHints] = useState<Hint[]>([]);
  const [autorunStatus, setAutorunStatus] = useState<Record<string, unknown> | null>(null);
  const [runtimeLogs, setRuntimeLogs] = useState<RuntimeLogs | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>('inspect');
  const [goal, setGoal] = useState('在授权范围内，使用 Kali 优先工具链解决 CTF/安全任务。');
  const [multiAgentExploration, setMultiAgentExploration] = useState(false);
  const [maxParallelExplorers, setMaxParallelExplorers] = useState(2);
  const [hintContent, setHintContent] = useState('先尝试 http://127.0.0.1/。');
  const [intentObjective, setIntentObjective] = useState('请求已授权的本地 HTTP 目标，并总结可观察的响应头。');
  const [intentTool, setIntentTool] = useState('http.request');
  const [intentRequest, setIntentRequest] = useState('{\n  "url": "http://127.0.0.1/",\n  "timeout_seconds": 3\n}');
  const [toolName, setToolName] = useState('sandbox.exec');
  const [toolRequest, setToolRequest] = useState('{\n  "command": "printf \'manual tool smoke test\\n\'",\n  "cwd": ".",\n  "timeout_seconds": 5\n}');
  const [browserSourceUrl, setBrowserSourceUrl] = useState('');
  const [browserCookie, setBrowserCookie] = useState('');
  const [manualTargetUrl, setManualTargetUrl] = useState('');
  const [artifactPreview, setArtifactPreview] = useState('');
  const [evidenceDrafts, setEvidenceDrafts] = useState<EvidenceDraft[]>([{ description: '', artifact_refs: [] }]);
  const [activeEvidenceIndex, setActiveEvidenceIndex] = useState(0);
  const [conclusion, setConclusion] = useState('');
  const [conclusionConfidence, setConclusionConfidence] = useState(0.7);
  const [conclusionCategory, setConclusionCategory] = useState('analysis');
  const [showHandsFree, setShowHandsFree] = useState(false);
  const [showNetworkProxy, setShowNetworkProxy] = useState(false);
  const [networkProxy, setNetworkProxy] = useState<NetworkProxyConfig>({ mode: 'system', proxy_url: '', no_proxy: '127.0.0.1,localhost,aurora-cc-switch' });
  const [concurrency, setConcurrency] = useState<ConcurrencyConfig>({ max_agents: 2, min: 1, max: 8, source: 'default', env_value: null });
  const [showTSecBench, setShowTSecBench] = useState(false);
  const [tsecbench, setTSecBench] = useState<TSecBenchConfig>({ base_url: 'https://tsecbench.zc.tencent.com', token_configured: false, token_source: 'none', timeout_seconds: 20, max_concurrent: 3, vpn_required: true });
  const [tsecbenchTest, setTSecBenchTest] = useState<TSecBenchTestResult | null>(null);
  const [showSlabMatch, setShowSlabMatch] = useState(false);
  const [slabMatch, setSlabMatch] = useState<SlabMatchConfig>({ base_url: 'https://example.com/slab-match/api/v1/agent', access_key_configured: false, access_key_source: 'none', timeout_seconds: 20, max_concurrent: 1, vpn_required: false });
  const [slabMatchTest, setSlabMatchTest] = useState<SlabMatchTestResult | null>(null);
  const [showOpenVPN, setShowOpenVPN] = useState(false);
  const [openvpn, setOpenVPN] = useState<OpenVPNConfig>({ configured: false, locked: false, connected: false, desired_connected: false, state: 'unconfigured', routes: [], credentials_configured: false });
  const [importUrl, setImportUrl] = useState('');
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([]);
  const [nameOverrides, setNameOverrides] = useState<Record<string, string>>({});
  const [importAuthMode, setImportAuthMode] = useState<'anonymous' | 'cookie' | 'password'>('anonymous');
  const [importCookie, setImportCookie] = useState('');
  const [importUsername, setImportUsername] = useState('');
  const [importPassword, setImportPassword] = useState('');
  const [importLoginUrl, setImportLoginUrl] = useState('');
  const [importFlagPrefixes, setImportFlagPrefixes] = useState('flag');
  const [importProgress, setImportProgress] = useState<ImportProgress>('idle');
  const [importProgressStep, setImportProgressStep] = useState(0);
  const [importProgressPercent, setImportProgressPercent] = useState(0);
  const [importProgressDetail, setImportProgressDetail] = useState('等待输入题目列表地址。');

  useEffect(() => {
    if (!importResult || importResult.batch.status !== 'SCANNING') return;
    const source = new EventSource(`${API_BASE}/api/hands-free/imports/${importResult.batch.id}/events`);
    source.addEventListener('progress', (event) => {
      const progressEvent = JSON.parse((event as MessageEvent).data) as { phase: string; detail: string };
      setImportProgressDetail(progressEvent.detail);
      const phaseProgress = IMPORT_PROGRESS_BY_PHASE[progressEvent.phase];
      if (phaseProgress) { setImportProgress('scanning'); setImportProgressStep(phaseProgress.step); setImportProgressPercent(phaseProgress.percent); return; }
      if (progressEvent.phase === 'READY' || progressEvent.phase === 'NEEDS_SESSION' || progressEvent.phase === 'FAILED') {
        source.close();
        void request<ImportResult>(`/api/hands-free/imports/${importResult.batch.id}`).then(updateImportProgress).catch((error) => { setImportProgress('failed'); setImportProgressDetail(error instanceof Error ? error.message : String(error)); });
      }
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setImportProgress('failed');
        setImportProgressDetail('实时状态连接已断开，请重新打开该导入批次检查结果。');
      }
    };
    return () => source.close();
  }, [importResult?.batch.id, importResult?.batch.status]);

  useEffect(() => { void loadProjects(); void loadGroups(); void loadNetworkProxy(); void loadTSecBench(); void loadSlabMatch(); void loadOpenVPN(); void loadConcurrency(); }, []);
  useEffect(() => { if (selectedProjectId) void loadProjectData(selectedProjectId); }, [selectedProjectId]);
  useEffect(() => { if (selectedGroupId) void loadGroup(selectedGroupId); else setGroupDetail(null); }, [selectedGroupId]);
  useEffect(() => { if (!selectedGroupId) return; const timer = window.setInterval(() => void loadGroup(selectedGroupId), 3000); return () => window.clearInterval(timer); }, [selectedGroupId]);
  useEffect(() => {
    if (!selectedProjectId) return;
    const timer = window.setInterval(() => void loadProjectData(selectedProjectId, true), 3000);
    return () => window.clearInterval(timer);
  }, [selectedProjectId]);
  useEffect(() => {
    if (!selectedProjectId) return;
    const source = new EventSource(`${API_BASE}/api/projects/${selectedProjectId}/events/stream`);
    source.addEventListener('project-event', (event) => {
      const incoming = JSON.parse((event as MessageEvent).data) as WorkerEvent;
      setEvents((current) => [incoming, ...current.filter((item) => item.id !== incoming.id)].sort((left, right) => right.created_at.localeCompare(left.created_at)));
      // Events are notifications only.  Fetch the authoritative blackboard so
      // node status and newly persisted evidence never depend on UI inference.
      if (eventRefreshTimer.current !== null) window.clearTimeout(eventRefreshTimer.current);
      eventRefreshTimer.current = window.setTimeout(() => {
        eventRefreshTimer.current = null;
        void loadProjectData(selectedProjectId, true);
      }, 100);
    });
    source.onerror = () => { /* EventSource reconnects automatically; polling remains the consistency fallback. */ };
    return () => { source.close(); if (eventRefreshTimer.current !== null) window.clearTimeout(eventRefreshTimer.current); };
  }, [selectedProjectId]);

  const latestTrace = traces[0];
  const latestContext = contexts[0];
  const projectLocked = ['COMPLETED', 'FAILED', 'CANCELLED', 'FLAG_READY', 'AWAITING_MANUAL_VALIDATION'].includes(blackboard?.project.status ?? '');
  const graph = useMemo(() => buildProgressGraph(blackboard, events, showHistory), [blackboard, events, showHistory]);
  const selected = useMemo(() => graph.nodes.find((node) => node.id === selectedNodeId)?.data as ProgressNodeData | undefined, [graph.nodes, selectedNodeId]);

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers = init?.body instanceof FormData ? undefined : { 'Content-Type': 'application/json' };
    const response = await fetch(`${API_BASE}${path}`, { headers, ...init });
    if (!response.ok) throw new Error((await response.text()) || `${response.status} ${response.statusText}`);
    return response.json() as Promise<T>;
  }
  async function loadProjects() {
    const data = await request<Project[]>('/api/projects');
    setProjects(data);
    if (!selectedProjectId && data.length) setSelectedProjectId(data[0].id);
  }
  async function loadGroups() {
    const data = await request<ChallengeGroup[]>('/api/challenge-groups');
    setGroups(data);
    if (!selectedGroupId && data.length) setSelectedGroupId(data[0].id);
  }
  async function loadNetworkProxy() { try { setNetworkProxy(await request<NetworkProxyConfig>('/api/settings/network-proxy')); } catch { /* Older API instances may require a restart after this frontend update. */ } }
  async function saveNetworkProxy(config: NetworkProxyConfig) { await guarded(async () => { const saved = await request<NetworkProxyConfig>('/api/settings/network-proxy', { method: 'PUT', body: JSON.stringify(config) }); setNetworkProxy(saved); setShowNetworkProxy(false); }); }
  async function loadConcurrency() { try { setConcurrency(await request<ConcurrencyConfig>('/api/settings/concurrency')); } catch { /* Older API instances may require a restart after this frontend update. */ } }
  async function saveConcurrency(next: number) { await guarded(async () => { const saved = await request<ConcurrencyConfig>('/api/settings/concurrency', { method: 'PUT', body: JSON.stringify({ max_agents: next }) }); setConcurrency(saved); }); }
  async function loadTSecBench() { try { setTSecBench(await request<TSecBenchConfig>('/api/settings/tsecbench')); } catch { /* Older API instances may require a restart after this frontend update. */ } }
  async function saveTSecBench(config: TSecBenchConfig & { token?: string; clear_token?: boolean }) { await guarded(async () => { const saved = await request<TSecBenchConfig>('/api/settings/tsecbench', { method: 'PUT', body: JSON.stringify(config) }); setTSecBench(saved); setTSecBenchTest(null); }); }
  async function testTSecBench() { await guarded(async () => { setTSecBenchTest(await request<TSecBenchTestResult>('/api/settings/tsecbench/test', { method: 'POST' })); }); }
  async function loadSlabMatch() { try { setSlabMatch(await request<SlabMatchConfig>('/api/settings/slab-match')); } catch { /* Older API instances may require a restart after this frontend update. */ } }
  async function saveSlabMatch(config: SlabMatchConfig & { access_key?: string; clear_access_key?: boolean }) { await guarded(async () => { const saved = await request<SlabMatchConfig>('/api/settings/slab-match', { method: 'PUT', body: JSON.stringify(config) }); setSlabMatch(saved); setSlabMatchTest(null); }); }
  async function testSlabMatch() { await guarded(async () => { setSlabMatchTest(await request<SlabMatchTestResult>('/api/settings/slab-match/test', { method: 'POST' })); }); }
  async function importSlabMatch(config: SlabMatchConfig & { access_key?: string; clear_access_key?: boolean }) { await guarded(async () => { const saved = await request<SlabMatchConfig>('/api/settings/slab-match', { method: 'PUT', body: JSON.stringify(config) }); setSlabMatch(saved); setSlabMatchTest(null); const imported = await request<SlabMatchImportResult>('/api/slab-match/import', { method: 'POST' }); await loadProjects(); await loadGroups(); setSelectedGroupId(imported.group.id); if (imported.projects.length) setSelectedProjectId(imported.projects[0].project_id); setMessage(''); setShowSlabMatch(false); }); }
  async function loadOpenVPN() { try { setOpenVPN(await request<OpenVPNConfig>('/api/settings/openvpn')); } catch { /* Older API instances may require a restart after this frontend update. */ } }
  async function saveOpenVPN(input: { file: File; vaultPassword: string; routes: string; username: string; password: string }) { await guarded(async () => { const form = new FormData(); form.set('ovpn', input.file); form.set('vault_password', input.vaultPassword); form.set('routes', input.routes); if (input.username) form.set('username', input.username); if (input.password) form.set('password', input.password); setOpenVPN(await request<OpenVPNConfig>('/api/settings/openvpn', { method: 'PUT', body: form })); }); }
  async function openvpnCommand(action: 'unlock' | 'lock' | 'connect' | 'disconnect', vaultPassword?: string) { await guarded(async () => { setOpenVPN(await request<OpenVPNConfig>(`/api/settings/openvpn/${action}`, { method: 'POST', body: action === 'unlock' ? JSON.stringify({ vault_password: vaultPassword }) : undefined })); }); }
  async function clearOpenVPN() { if (!window.confirm('清除加密保存的 OVPN 和凭据？该操作不可恢复。')) return; await guarded(async () => { setOpenVPN(await request<OpenVPNConfig>('/api/settings/openvpn', { method: 'DELETE' })); }); }
  async function loadGroup(groupId: string) {
    const detail = await request<ChallengeGroupDetail>(`/api/challenge-groups/${groupId}`);
    setGroupDetail(detail);
    return detail;
  }
  function selectNextGroupProject(detail: ChallengeGroupDetail, completedProjectId: string) {
    const nextItem = detail.items.find((item) => item.project_id !== completedProjectId && !['COMPLETED', 'FAILED'].includes(item.fused_status || item.status));
    if (nextItem) setSelectedProjectId(nextItem.project_id);
    return Boolean(nextItem);
  }
  async function loadProjectData(projectId: string, silent = false) {
    const [board, contextData, traceData, toolData, eventData, warningData, targetData, hintData, autoData, logs] = await Promise.all([
      request<Blackboard>(`/api/projects/${projectId}/blackboard`), request<ContextSnapshot[]>(`/api/projects/${projectId}/debug/context-snapshots`), request<LLMTrace[]>(`/api/projects/${projectId}/debug/llm-traces`), request<ToolTrace[]>(`/api/projects/${projectId}/debug/tool-traces`), request<WorkerEvent[]>(`/api/projects/${projectId}/events`), request<WorkerEvent[]>(`/api/projects/${projectId}/warnings`), request<DiscoveredTarget[]>(`/api/projects/${projectId}/targets`), request<Hint[]>(`/api/projects/${projectId}/hints`), request<Record<string, unknown>>(`/api/projects/${projectId}/autorun/status`), request<RuntimeLogs>(`/api/projects/${projectId}/runtime/logs?tail=120`),
    ]);
    setBlackboard(board); setContexts(contextData); setTraces(traceData); setToolTraces(toolData); setEvents(eventData); setWarnings(warningData); setTargets(targetData); setHints(hintData); setAutorunStatus(autoData); setRuntimeLogs(logs);
    if (silent) setSelectedNodeId((current) => current && graphEntityExists(current, board) ? current : null);
  }
  async function guarded(action: () => Promise<void>) {
    setBusy(true); setMessage('');
    try { await action(); } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); } finally { setBusy(false); }
  }
  const refresh = () => selectedProjectId ? loadProjectData(selectedProjectId) : Promise.resolve();
  async function createProject() {
    await guarded(async () => { const project = await request<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name: `Operation ${new Date().toLocaleTimeString()}`, goal, allowed_hosts: ['127.0.0.1'], multi_agent_exploration_enabled: multiAgentExploration, max_parallel_explorers: maxParallelExplorers }) }); await loadProjects(); setSelectedProjectId(project.id); });
  }
  async function command(path: string, body?: Record<string, unknown>) {
    if (!selectedProjectId) return;
    await guarded(async () => { await request(`/api/projects/${selectedProjectId}${path}`, { method: 'POST', body: body ? JSON.stringify(body) : undefined }); await refresh(); });
  }
  async function createHint() { await command('/hints', { content: hintContent, source: 'user' }); }
  async function createIntent() { await command('/intents', { objective: intentObjective, capability_tags: [intentTool], priority: 2, risk_level: 'low', tool_request: JSON.parse(intentRequest) }); }
  async function executeTool() { await command(`/tools/${toolName}/execute`, { request: JSON.parse(toolRequest) }); }
  async function setBrowserSession() { if (!selectedProjectId || !browserSourceUrl.trim() || !browserCookie.trim()) return; await guarded(async () => { await request(`/api/projects/${selectedProjectId}/browser/session`, { method: 'POST', body: JSON.stringify({ source_url: browserSourceUrl.trim(), cookie: browserCookie.trim() }) }); setBrowserCookie(''); await refresh(); }); }
  async function verifyTarget(confirmPaid = false) { await command('/target/verify', { confirm_paid: confirmPaid }); }
  async function submitManualTarget() {
    if (!selectedProjectId || !manualTargetUrl.trim()) return;
    await guarded(async () => {
      await request(`/api/projects/${selectedProjectId}/targets/manual`, { method: 'POST', body: JSON.stringify({ url: manualTargetUrl.trim(), probe: true }) });
      setManualTargetUrl('');
      await refresh();
    });
  }
  async function confirmTarget(targetId: string) {
    if (!selectedProjectId) return;
    await guarded(async () => { await request(`/api/projects/${selectedProjectId}/targets/${targetId}/confirm`, { method: 'POST' }); await refresh(); });
  }
  async function rethinkProject() {
    if (!selectedProjectId || !window.confirm('再次思考会停止当前执行，清空工作黑板，并保留原始证据与审计记录。是否继续？')) return;
    await guarded(async () => {
      await request(`/api/projects/${selectedProjectId}/rethink`, { method: 'POST' });
      setBlackboard((current) => current ? { ...current, project: { ...current.project, status: 'WORKING' }, facts: [], intents: [], attempts: [], findings: [], flag_candidates: [], workers: [], checkpoints: [] } : current);
      setEvidenceDrafts([{ description: '', artifact_refs: [] }]); setActiveEvidenceIndex(0); setConclusion(''); setArtifactPreview(''); setSelectedNodeId(null);
      await refresh();
    });
  }
  async function deriveConclusion() {
    const evidenceItems = evidenceDrafts.map((item) => ({ description: item.description.trim(), artifact_refs: item.artifact_refs }));
    if (!selectedProjectId || !conclusion.trim() || !evidenceItems.length || evidenceItems.some((item) => !item.description || !item.artifact_refs.length)) return;
    await guarded(async () => {
      await request(`/api/projects/${selectedProjectId}/facts`, { method: 'POST', body: JSON.stringify({ statement: conclusion.trim(), evidence_items: evidenceItems, confidence: conclusionConfidence, category: conclusionCategory.trim() || 'analysis' }) });
      setConclusion(''); setEvidenceDrafts([{ description: '', artifact_refs: [] }]); setActiveEvidenceIndex(0); await refresh();
    });
  }
  function updateEvidenceDescription(index: number, description: string) { setEvidenceDrafts((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, description } : item)); }
  function toggleEvidenceArtifact(index: number, artifactId: string) { setEvidenceDrafts((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, artifact_refs: item.artifact_refs.includes(artifactId) ? item.artifact_refs.filter((id) => id !== artifactId) : [...item.artifact_refs, artifactId] } : item)); }
  function addEvidenceDraft() { if (evidenceDrafts.length >= 10) return; setEvidenceDrafts((current) => [...current, { description: '', artifact_refs: [] }]); setActiveEvidenceIndex(evidenceDrafts.length); }
  function removeEvidenceDraft(index: number) { setEvidenceDrafts((current) => current.length === 1 ? current : current.filter((_, itemIndex) => itemIndex !== index)); setActiveEvidenceIndex((current) => Math.max(0, current > index ? current - 1 : Math.min(current, evidenceDrafts.length - 2))); }
  async function acknowledgeWarning(id: string) { await command(`/warnings/${id}/acknowledge`); }
  async function previewArtifact(id: string) { const data = await request<{ content: string; truncated: boolean }>(`/api/artifacts/${id}/content?max_bytes=12000`); setArtifactPreview(`${data.content}${data.truncated ? '\n\n[truncated]' : ''}`); setDrawerTab('evidence'); }
  function importAuthPayload() { return importAuthMode === 'cookie' ? { cookie: importCookie } : importAuthMode === 'password' ? { username: importUsername, password: importPassword, login_url: importLoginUrl || undefined } : {}; }
  function useImportResult(result: ImportResult) { setImportResult(result); setSelectedCandidates(result.candidates.map((candidate) => candidate.id)); setNameOverrides(Object.fromEntries(result.candidates.map((candidate) => [candidate.id, candidate.title]))); }
  function updateImportProgress(result: ImportResult) { useImportResult(result); if (result.batch.status === 'SCANNING') { setImportProgress('scanning'); setImportProgressDetail('导入任务已开始，正在等待实时状态。'); } else if (result.batch.status === 'NEEDS_SESSION') { setImportProgress('needs_session'); setImportProgressDetail(result.batch.auth_message || '需要有效的登录会话才能继续。'); } else if (result.batch.status === 'FAILED') { setImportProgress('failed'); setImportProgressDetail(result.batch.error || '导入失败，请检查返回信息。'); } else { setImportProgress('ready'); setImportProgressPercent(100); setImportProgressStep(IMPORT_PROGRESS_STEPS.length - 1); setImportProgressDetail(result.candidates.length ? `已识别 ${result.candidates.length} 个可信候选题目，可确认批量创建项目。` : '采集完成，但没有发现可验证的题目候选。'); } }
  async function runImport(path: string, body: Record<string, unknown>) { setBusy(true); setMessage(''); setImportProgress('scanning'); setImportProgressStep(0); setImportProgressPercent(2); setImportProgressDetail('正在建立导入任务，请保持此窗口打开。'); try { updateImportProgress(await request<ImportResult>(path, { method: 'POST', body: JSON.stringify(body) })); } catch (error) { const detail = error instanceof Error ? error.message : String(error); setMessage(detail); setImportProgress('failed'); setImportProgressDetail(detail); } finally { setImportCookie(''); setImportPassword(''); setBusy(false); } }
  async function scanImport() { await runImport('/api/hands-free/imports', { source_url: importUrl, ...importAuthPayload() }); }
  async function continueImport() { if (!importResult) return; await runImport(`/api/hands-free/imports/${importResult.batch.id}/continue`, importAuthPayload()); }
  async function confirmImport() { if (!importResult) return; await guarded(async () => { const flag_prefixes = importFlagPrefixes.split(',').map((value) => value.trim()).filter(Boolean); const result = await request<{ projects: Array<{ project_id: string }>; group?: ChallengeGroup }>(`/api/hands-free/imports/${importResult.batch.id}/confirm`, { method: 'POST', body: JSON.stringify({ candidate_ids: selectedCandidates, name_overrides: nameOverrides, flag_prefixes }) }); await loadProjects(); await loadGroups(); if (result.projects[0]?.project_id) setSelectedProjectId(result.projects[0].project_id); if (result.group?.id) setSelectedGroupId(result.group.id); setShowHandsFree(false); setImportResult(null); }); }
  async function groupCommand(path: string) { if (!selectedGroupId) return; await guarded(async () => { await request(`/api/challenge-groups/${selectedGroupId}${path}`, { method: 'POST' }); await loadGroups(); await loadGroup(selectedGroupId); }); }
  async function repairSlabMatchAttachments() { if (!selectedGroupId) return; await guarded(async () => { const result = await request<SlabMatchAttachmentRepairResult>(`/api/slab-match/groups/${selectedGroupId}/attachments/repair`, { method: 'POST' }); await loadGroup(selectedGroupId); if (selectedProjectId) await loadProjectData(selectedProjectId); if (result.failures.length) setMessage(`${result.artifact_count} 个附件已补入，${result.failures.length} 个下载失败`); }); }
  async function validateFlagManually(itemId: string, accepted: boolean) {
    if (!selectedGroupId) return;
    await guarded(async () => {
      const reviewedProjectId = groupDetail?.items.find((item) => item.id === itemId)?.project_id;
      await request(`/api/challenge-groups/${selectedGroupId}/items/${itemId}/flag-validation`, { method: 'POST', body: JSON.stringify({ accepted }) });
      await loadGroups(); const detail = await loadGroup(selectedGroupId); await loadProjects();
      if (accepted && reviewedProjectId && selectNextGroupProject(detail, reviewedProjectId)) return;
      if (selectedProjectId) await refresh();
    });
  }
  async function validateProjectFlag(candidateId: string, accepted: boolean) {
    if (!selectedProjectId) return;
    await guarded(async () => {
      const reviewedProjectId = selectedProjectId;
      await request(`/api/projects/${reviewedProjectId}/flag-candidates/${candidateId}/validation`, { method: 'POST', body: JSON.stringify({ accepted }) });
      await loadProjects();
      const detail = selectedGroupId ? await loadGroup(selectedGroupId) : null;
      if (accepted && detail && selectNextGroupProject(detail, reviewedProjectId)) return;
      await loadProjectData(reviewedProjectId);
    });
  }
  async function deleteProject() {
    if (!selectedProjectId || !window.confirm('永久删除此任务及其证据、执行记录和本地工作目录？该操作不可恢复。')) return;
    await guarded(async () => {
      await request(`/api/projects/${selectedProjectId}`, { method: 'DELETE' });
      setBlackboard(null); setEvents([]); setWarnings([]); setSelectedNodeId(null); setEvidenceDrafts([{ description: '', artifact_refs: [] }]); setActiveEvidenceIndex(0); setArtifactPreview('');
      const [nextProjects, nextGroups] = await Promise.all([request<Project[]>('/api/projects'), request<ChallengeGroup[]>('/api/challenge-groups')]);
      setProjects(nextProjects); setGroups(nextGroups); setSelectedProjectId(nextProjects[0]?.id ?? '');
      if (!nextGroups.some((group) => group.id === selectedGroupId)) setSelectedGroupId(nextGroups[0]?.id ?? '');
    });
  }
  async function deleteGroup() {
    if (!selectedGroupId || !window.confirm('永久删除此任务组及其中所有任务、证据和执行记录？共享任务也会从其他任务组移除。该操作不可恢复。')) return;
    await guarded(async () => {
      await request(`/api/challenge-groups/${selectedGroupId}`, { method: 'DELETE' });
      setGroupDetail(null); setBlackboard(null); setEvents([]); setWarnings([]); setSelectedNodeId(null); setEvidenceDrafts([{ description: '', artifact_refs: [] }]); setActiveEvidenceIndex(0); setArtifactPreview('');
      const [nextProjects, nextGroups] = await Promise.all([request<Project[]>('/api/projects'), request<ChallengeGroup[]>('/api/challenge-groups')]);
      setProjects(nextProjects); setGroups(nextGroups); setSelectedProjectId(nextProjects[0]?.id ?? ''); setSelectedGroupId(nextGroups[0]?.id ?? '');
    });
  }

  return <>
  <main className="ops-shell">
    <aside className="command-rail">
      <div className="brand"><span className="brand-mark"><Network size={21} /></span><div><span>Aurora</span><strong>Operation Grid</strong></div></div>
      <section className="project-switcher"><label>项目</label><select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}><option value="">选择项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select><textarea value={goal} onChange={(event) => setGoal(event.target.value)} aria-label="项目目标" /><div className="multi-agent-control"><label><input type="checkbox" checked={multiAgentExploration} onChange={(event) => setMultiAgentExploration(event.target.checked)} />多 Agent 探索</label><input type="number" min={1} max={8} value={maxParallelExplorers} disabled={!multiAgentExploration} onChange={(event) => setMaxParallelExplorers(Math.max(1, Math.min(8, Number(event.target.value) || 1)))} aria-label="项目并行 Explore Agent 数" title="项目并行 Explore Agent 数" /></div><button className="primary-button" onClick={() => void createProject()} disabled={busy}><Plus size={16} />新建项目</button></section>
      <section className="group-console"><label>题目组</label><select value={selectedGroupId} onChange={(event) => setSelectedGroupId(event.target.value)}><option value="">选择题目组</option>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select><ConcurrencyControl config={concurrency} busy={busy} onSave={(next) => void saveConcurrency(next)} />{groupDetail ? <><p>{groupDetail.group.status} · {groupDetail.items.filter((item) => (item.fused_status || item.status) === 'COMPLETED').length}/{groupDetail.items.length} · 并发 {groupDetail.group.max_concurrent ?? 1}</p><div><button className="primary-button" disabled={busy || ['COMPLETED', 'AWAITING_MANUAL_VALIDATION'].includes(groupDetail.group.status)} onClick={() => void groupCommand('/start')}><Play size={15} />开始组解题</button><button disabled={busy} onClick={() => void groupCommand('/stop')}><PauseCircle size={15} />停止</button>{groupDetail.group.limits?.platform === 'slab_match' ? <button disabled={busy} title="串行刷新并补入缺失附件" onClick={() => void repairSlabMatchAttachments()}><Download size={15} />修复附件</button> : null}<button className="danger-button" title="永久删除任务组" aria-label="永久删除任务组" disabled={busy} onClick={() => void deleteGroup()}><Trash2 size={15} /></button></div><ol>{groupDetail.items.map((item) => <li key={item.id} className={(item.fused_status || item.status).toLowerCase()}><button onClick={() => setSelectedProjectId(item.project_id)}>{item.position}. {groupDetail.projects[item.project_id]?.name || item.project_id}</button><small>P{item.phase ?? 1} · {item.fused_status || item.status}{item.hint_taken ? ' · 已取提示' : ''}{item.stop_reason ? ` · ${item.stop_reason}` : ''}</small>{item.submission_status === 'AWAITING_MANUAL_VALIDATION' ? <div className="manual-flag-validation"><code>{groupDetail.candidate_flags[item.project_id] || '候选 flag'}</code><span>提交接口不可用，请人工核验：</span><button className="primary-button" disabled={busy} onClick={() => void validateFlagManually(item.id, true)}><Check size={14} />正确</button><button className="danger-button" disabled={busy} onClick={() => void validateFlagManually(item.id, false)}><X size={14} />错误</button></div> : null}</li>)}</ol></> : null}</section>
      <nav className="command-actions" aria-label="项目控制">
        <IconAction icon={<FolderInput />} label="解放双手" disabled={busy} onClick={() => setShowHandsFree(true)} />
        <IconAction icon={<Network />} label="网络代理" disabled={busy} onClick={() => setShowNetworkProxy(true)} />
        <IconAction icon={<ShieldCheck />} label={`OpenVPN${openvpn.connected ? ' · 已连接' : ''}`} disabled={busy} onClick={() => { setMessage(''); void loadOpenVPN(); setShowOpenVPN(true); }} />
        <IconAction icon={<ShieldCheck />} label="TSecBench" disabled={busy} onClick={() => { setMessage(''); setShowTSecBench(true); }} />
        <IconAction icon={<Network />} label="Slab Match" disabled={busy} onClick={() => { setMessage(''); void loadSlabMatch(); setShowSlabMatch(true); }} />
        <IconAction icon={<Trash2 />} label="删除任务" disabled={busy || !selectedProjectId} onClick={() => void deleteProject()} danger />
        <IconAction icon={<RotateCcw />} label="再次思考" disabled={busy || !selectedProjectId} onClick={() => void rethinkProject()} />
        <IconAction icon={<BrainCircuit />} label="运行规划器" disabled={busy || !selectedProjectId} onClick={() => void command('/manager/run')} />
        <IconAction icon={<Play />} label="执行下一个意图" disabled={busy || !selectedProjectId || projectLocked} onClick={() => void command('/scheduler/run-next')} />
        <IconAction icon={<ShieldCheck />} label="运行观察器" disabled={busy || !selectedProjectId} onClick={() => void command('/observer/run')} />
        <IconAction icon={<Sparkles />} label="开始自动解题" disabled={busy || !selectedProjectId || projectLocked} onClick={() => void command('/autorun/start', { max_iterations: 0, max_minutes: 0, no_progress_limit: 2, stop_on_observer_escalate: true, background: true })} />
        <IconAction icon={<PauseCircle />} label="停止自动解题" disabled={busy || !selectedProjectId} onClick={() => void command('/autorun/stop')} danger />
        <IconAction icon={<TimerReset />} label="回收过期租约" disabled={busy || !selectedProjectId} onClick={() => void command('/scheduler/reap-expired')} />
      </nav>
      <section className="hint-console"><label>人工线索</label><textarea value={hintContent} onChange={(event) => setHintContent(event.target.value)} /><button onClick={() => void createHint()} disabled={busy || projectLocked}><ChevronRight size={16} />注入</button><div className="hint-feed">{hints.slice(0, 4).map((hint) => <p key={hint.id} className={hint.consumed ? 'consumed' : ''}>{hint.content}</p>)}</div></section>
    </aside>

    <section className="workspace">
      <header className="workspace-header"><div><span className="eyebrow">LIVE EXECUTION GRAPH</span><h1>{blackboard?.project.name ?? '未选择项目'}</h1>{selectedProjectId ? <code className="project-id-label">project-id: {selectedProjectId}</code> : null}<p>{blackboard?.project.goal ?? '创建或选择一个项目后开始。'}</p></div><div className="header-actions"><span className={`project-status ${String(blackboard?.project.status ?? '').toLowerCase()}`}>{statusLabel(blackboard?.project.status ?? 'IDLE')}</span><button className="icon-button" title="刷新项目数据" aria-label="刷新项目数据" disabled={busy || !selectedProjectId} onClick={() => void refresh()}><RefreshCw size={17} /></button></div></header>
      {message ? <div className="notice error">{message}</div> : null}
      {warnings.length ? <section className="runtime-warning" aria-live="assertive"><AlertTriangle size={18} /><div><strong>运行时错误</strong><p>{String(warnings[0].payload_json.error ?? 'Worker 运行失败。')}</p><small>{new Date(warnings[0].created_at).toLocaleString()}</small></div><button title="确认并隐藏警告" aria-label="确认并隐藏警告" disabled={busy} onClick={() => void acknowledgeWarning(warnings[0].id)}><Check size={16} /></button></section> : null}
      {selectedProjectId ? <TargetControlPanel project={blackboard?.project} targets={targets} manualUrl={manualTargetUrl} setManualUrl={setManualTargetUrl} busy={busy} onManual={() => void submitManualTarget()} onVerify={() => void verifyTarget()} onConfirmPaid={() => void verifyTarget(true)} onConfirm={(targetId) => void confirmTarget(targetId)} /> : null}
      {blackboard?.flag_candidates.filter((candidate) => ['LOCAL_VERIFIED', 'AWAITING_MANUAL_VALIDATION'].includes(candidate.status)).map((candidate) => <section className="flag-candidate-review" key={candidate.id}><ShieldCheck size={18} /><div><strong>{candidate.value}</strong><p>{statusLabel(candidate.status)} · {candidate.provenance_kind} · 已提交 {candidate.submission_count} 次</p></div><button className="primary-button" disabled={busy} onClick={() => void validateProjectFlag(candidate.id, true)}><Check size={15} />接受</button><button className="danger-button" disabled={busy} onClick={() => void validateProjectFlag(candidate.id, false)}><X size={15} />拒绝</button></section>)}
      <section className="telemetry-strip"><Metric icon={<Boxes size={16} />} label="意图" value={blackboard?.intents.length ?? 0} /><Metric icon={<Bot size={16} />} label={blackboard?.runtime_policy?.multi_agent_exploration_enabled ? `并行 Worker / ${blackboard.runtime_policy.max_parallel_explorers}` : 'Worker'} value={blackboard?.workers.filter((worker) => ['STARTING', 'RUNNING', 'CONCLUDING'].includes(worker.status)).length ?? 0} /><Metric icon={<CircleDot size={16} />} label="尝试" value={blackboard?.attempts.length ?? 0} /><Metric icon={<FileSearch size={16} />} label="证据" value={blackboard?.artifacts.length ?? 0} /><Metric icon={<Flag size={16} />} label="发现" value={blackboard?.findings.length ?? 0} accent /><Metric icon={<History size={16} />} label="图版本" value={blackboard?.coordination?.graph_version ?? 0} /></section>
      <section className="graph-workspace">
        <div className="graph-toolbar"><div><span>执行拓扑</span><small>{latestContext ? `${latestContext.estimated_tokens} token context · ${targets.filter((target) => target.status === 'ACTIVE').length} 个靶机` : '等待上下文'}</small></div><div className="segmented"><button className={!showHistory ? 'selected' : ''} onClick={() => setShowHistory(false)}>活跃路径</button><button className={showHistory ? 'selected' : ''} onClick={() => setShowHistory(true)}>完整历史</button></div></div>
        {blackboard ? <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={{ progress: ProgressNode }} onNodeClick={(_, node) => { setSelectedNodeId(node.id); setDrawerTab('inspect'); }} onPaneClick={() => setSelectedNodeId(null)} fitView minZoom={0.2} maxZoom={1.8} proOptions={{ hideAttribution: true }}><Background gap={22} size={1} color="#203044" /><MiniMap pannable zoomable nodeColor={(node) => node.data.kind === 'finding' ? '#e7a94a' : node.data.kind === 'worker' ? '#4ac5d8' : '#6077ea'} /><Controls showInteractive={false} /></ReactFlow> : <div className="graph-empty">选择项目后显示任务执行图。</div>}
        <div className="graph-legend"><span><i className="intent-dot" />意图</span><span><i className="worker-dot" />Worker</span><span><i className="attempt-dot" />尝试</span><span><i className="hypothesis-dot" />假设</span><span><i className="evidence-dot" />证据</span><span><i className="finding-dot" />发现</span></div>
      </section>
      <LiveActionStrip events={events} onSelect={(event) => { setSelectedNodeId(event.worker_id ? `worker:${event.worker_id}` : event.attempt_id ? `attempt:${event.attempt_id}` : null); setDrawerTab('inspect'); }} />
    </section>

    <aside className="inspector-rail">
      <div className="inspector-tabs"><Tab icon={<FileSearch size={16} />} label="检查" active={drawerTab === 'inspect'} onClick={() => setDrawerTab('inspect')} /><Tab icon={<Wrench size={16} />} label="控制" active={drawerTab === 'controls'} onClick={() => setDrawerTab('controls')} /><Tab icon={<TerminalSquare size={16} />} label="证据" active={drawerTab === 'evidence'} onClick={() => setDrawerTab('evidence')} /></div>
      {drawerTab === 'inspect' ? <InspectPanel selected={selected} onClear={() => setSelectedNodeId(null)} board={blackboard} events={events} latestTrace={latestTrace} runtimeLogs={runtimeLogs} /> : null}
      {drawerTab === 'controls' ? <ControlPanel busy={busy} locked={projectLocked} intentObjective={intentObjective} setIntentObjective={setIntentObjective} intentTool={intentTool} setIntentTool={setIntentTool} intentRequest={intentRequest} setIntentRequest={setIntentRequest} toolName={toolName} setToolName={setToolName} toolRequest={toolRequest} setToolRequest={setToolRequest} browserSourceUrl={browserSourceUrl} setBrowserSourceUrl={setBrowserSourceUrl} browserCookie={browserCookie} setBrowserCookie={setBrowserCookie} onBrowserSession={() => void setBrowserSession()} onIntent={() => void createIntent()} onTool={() => void executeTool()} /> : null}
      {drawerTab === 'evidence' ? <ConclusionEvidencePanel board={blackboard} preview={artifactPreview} onPreview={(id) => void previewArtifact(id)} evidenceDrafts={evidenceDrafts} activeEvidenceIndex={activeEvidenceIndex} onSelectEvidence={setActiveEvidenceIndex} onDescription={updateEvidenceDescription} onToggleArtifact={toggleEvidenceArtifact} onAddEvidence={addEvidenceDraft} onRemoveEvidence={removeEvidenceDraft} conclusion={conclusion} setConclusion={setConclusion} confidence={conclusionConfidence} setConfidence={setConclusionConfidence} category={conclusionCategory} setCategory={setConclusionCategory} onDerive={() => void deriveConclusion()} disabled={busy || projectLocked} /> : null}
    </aside>
  </main>
  {showHandsFree ? <HandsFreeDialog busy={busy} url={importUrl} setUrl={setImportUrl} result={importResult} selected={selectedCandidates} setSelected={setSelectedCandidates} names={nameOverrides} setNames={setNameOverrides} authMode={importAuthMode} setAuthMode={setImportAuthMode} cookie={importCookie} setCookie={setImportCookie} username={importUsername} setUsername={setImportUsername} password={importPassword} setPassword={setImportPassword} loginUrl={importLoginUrl} setLoginUrl={setImportLoginUrl} flagPrefixes={importFlagPrefixes} setFlagPrefixes={setImportFlagPrefixes} progress={importProgress} progressStep={importProgressStep} progressPercent={importProgressPercent} progressDetail={importProgressDetail} onScan={() => void scanImport()} onContinue={() => void continueImport()} onConfirm={() => void confirmImport()} onClose={() => { setImportCookie(''); setImportPassword(''); setImportFlagPrefixes('flag'); setShowHandsFree(false); }} /> : null}
  {showNetworkProxy ? <NetworkProxyDialog busy={busy} config={networkProxy} onSave={(config) => void saveNetworkProxy(config)} onClose={() => setShowNetworkProxy(false)} /> : null}
  {showOpenVPN ? <OpenVPNDialog busy={busy} config={openvpn} error={message} onSave={(input) => void saveOpenVPN(input)} onCommand={(action, password) => void openvpnCommand(action, password)} onClear={() => void clearOpenVPN()} onClose={() => setShowOpenVPN(false)} /> : null}
  {showTSecBench ? <TSecBenchDialog busy={busy} config={tsecbench} testResult={tsecbenchTest} error={message} onSave={(config) => void saveTSecBench(config)} onTest={() => void testTSecBench()} onClose={() => setShowTSecBench(false)} /> : null}
  {showSlabMatch ? <SlabMatchDialog busy={busy} config={slabMatch} testResult={slabMatchTest} error={message} onSave={(config) => void saveSlabMatch(config)} onImport={(config) => void importSlabMatch(config)} onTest={() => void testSlabMatch()} onClose={() => setShowSlabMatch(false)} /> : null}
  </>;
}

function buildProgressGraph(board: Blackboard | null, events: WorkerEvent[], showHistory: boolean): { nodes: Node<ProgressNodeData>[]; edges: Edge[] } {
  if (!board) return { nodes: [], edges: [] };
  const activeIntentIds = new Set(board.intents.filter((intent) => activeStatuses.has(intent.status)).map((intent) => intent.id));
  if (!showHistory && !activeIntentIds.size) board.intents.slice(-6).forEach((intent) => activeIntentIds.add(intent.id));
  const visibleIntents = showHistory ? board.intents : board.intents.filter((intent) => activeIntentIds.has(intent.id) || activeIntentIds.has(intent.parent_intent_id ?? ''));
  const intentIds = new Set(visibleIntents.map((item) => item.id));
  const workers = board.workers.filter((worker) => showHistory || intentIds.has(worker.intent_id));
  const workerIds = new Set(workers.map((item) => item.id));
  const attempts = board.attempts.filter((attempt) => showHistory || workerIds.has(attempt.worker_id));
  const attemptIds = new Set(attempts.map((item) => item.id));
  const facts = board.facts.filter((fact) => showHistory || !fact.source_attempt_id || attemptIds.has(fact.source_attempt_id));
  const artifacts = board.artifacts.filter((artifact) => showHistory || !artifact.source_attempt_id || attemptIds.has(artifact.source_attempt_id));
  const checkpoints = board.checkpoints.filter((checkpoint) => showHistory || attemptIds.has(checkpoint.attempt_id));
  const hypotheses = checkpoints.flatMap((checkpoint) => checkpoint.hypotheses.map((hypothesis, index) => ({ id: `hypothesis:${checkpoint.id}:${index}`, checkpointId: checkpoint.id, label: hypothesis, meta: `来自轮次 ${shortId(checkpoint.id)}`, status: 'HYPOTHESIS' })));
  const evidenceIds = new Set([...facts.flatMap((fact) => fact.evidence_refs), ...artifacts.map((artifact) => artifact.id)]);
  const findings = board.findings.filter((finding) => showHistory || finding.evidence_refs.some((id) => evidenceIds.has(id)));
  const activities = events.filter((event) => event.attempt_id && (event.event_type === 'tool.started' || event.event_type === 'tool.executed')).filter((event) => showHistory || attemptIds.has(event.attempt_id!));
  const groups: Array<[ProgressNodeData['kind'], Array<{ id: string; label: string; meta: string; status: string }>]> = [
    ['intent', visibleIntents.map((intent) => ({ id: `intent:${intent.id}`, label: intent.objective, meta: `P${intent.priority.toFixed(1)} · ${shortId(intent.id)}`, status: intent.status }))],
    ['worker', workers.map((worker) => ({ id: `worker:${worker.id}`, label: worker.execution_kind === 'subagent' ? 'Subagent Worker' : 'Solver Worker', meta: `${worker.agent_profile_id ?? 'solver.general'} · ${shortId(worker.id)}`, status: worker.status }))],
    ['attempt', attempts.map((attempt) => ({ id: `attempt:${attempt.id}`, label: attempt.result_summary || '执行尝试', meta: `${shortId(attempt.id)} · 恢复 ${attempt.resume_count ?? 0} · 黑板 v${attempt.blackboard_version ?? 0}`, status: attempt.status }))],
    ['activity', activities.map((event) => ({ id: `activity:${event.id}`, label: String(event.payload_json.activity_label ?? event.payload_json.tool_name ?? '正在执行工具操作'), meta: String(event.payload_json.tool_name ?? 'tool'), status: event.event_type === 'tool.started' ? 'RUNNING' : event.payload_json.success === false ? 'FAILED' : 'COMPLETED' }))],
    ['checkpoint', checkpoints.map((checkpoint) => ({ id: `checkpoint:${checkpoint.id}`, label: checkpoint.summary, meta: `${checkpoint.source} · ${checkpoint.generated_intent_ids.length} 个后续意图`, status: checkpoint.status }))],
    ['hypothesis', hypotheses],
    ['artifact', artifacts.map((artifact) => ({ id: `artifact:${artifact.id}`, label: artifact.summary || artifact.type, meta: `${artifact.type} · ${formatBytes(artifact.size)}`, status: 'EVIDENCE' }))],
    ['fact', facts.map((fact) => ({ id: `fact:${fact.id}`, label: fact.statement, meta: `${Math.round(fact.confidence * 100)}% confidence`, status: 'FACT' }))],
    ['finding', findings.map((finding) => ({ id: `finding:${finding.id}`, label: finding.title, meta: finding.severity, status: 'FINDING' }))],
  ];
  const columns: Record<string, number> = { intent: 0, worker: 240, attempt: 480, activity: 720, checkpoint: 960, hypothesis: 1200, artifact: 1440, fact: 1680, finding: 1920 };
  const nodes = groups.flatMap(([kind, items]) => items.map((item, index) => ({ id: item.id, type: 'progress', position: { x: columns[kind], y: 36 + index * 142 }, data: { kind, label: item.label, meta: item.meta, status: item.status, entityId: item.id.split(':')[1] } })));
  const edges: Edge[] = [];
  const connect = (source: string, target: string, kind = 'flow') => { if (nodes.some((node) => node.id === source) && nodes.some((node) => node.id === target)) edges.push({ id: `${source}-${target}`, source, target, type: 'smoothstep', animated: kind === 'active', markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 }, className: `edge-${kind}` }); };
  visibleIntents.forEach((intent) => { if (intent.parent_intent_id) connect(`intent:${intent.parent_intent_id}`, `intent:${intent.id}`, 'branch'); });
  workers.forEach((worker) => { connect(`intent:${worker.intent_id}`, `worker:${worker.id}`, activeStatuses.has(worker.status) ? 'active' : 'flow'); if (worker.parent_worker_id) connect(`worker:${worker.parent_worker_id}`, `worker:${worker.id}`, 'branch'); });
  attempts.forEach((attempt) => { connect(`worker:${attempt.worker_id}`, `attempt:${attempt.id}`, activeStatuses.has(attempt.status) ? 'active' : 'flow'); if (attempt.parent_attempt_id) connect(`attempt:${attempt.parent_attempt_id}`, `attempt:${attempt.id}`, 'branch'); });
  activities.forEach((event) => connect(`attempt:${event.attempt_id}`, `activity:${event.id}`, event.event_type === 'tool.started' ? 'active' : 'flow'));
  checkpoints.forEach((checkpoint) => { connect(`attempt:${checkpoint.attempt_id}`, `checkpoint:${checkpoint.id}`); if (checkpoint.parent_checkpoint_id) connect(`checkpoint:${checkpoint.parent_checkpoint_id}`, `checkpoint:${checkpoint.id}`, 'branch'); checkpoint.generated_intent_ids.forEach((id) => connect(`checkpoint:${checkpoint.id}`, `intent:${id}`, 'branch')); checkpoint.fact_refs.forEach((id) => connect(`fact:${id}`, `checkpoint:${checkpoint.id}`)); checkpoint.artifact_refs.forEach((id) => connect(`artifact:${id}`, `checkpoint:${checkpoint.id}`)); });
  hypotheses.forEach((hypothesis) => connect(`checkpoint:${hypothesis.checkpointId}`, hypothesis.id, 'hypothesis'));
  artifacts.forEach((artifact) => { if (artifact.source_attempt_id) connect(`attempt:${artifact.source_attempt_id}`, `artifact:${artifact.id}`); });
  facts.forEach((fact) => { if (fact.source_attempt_id) connect(`attempt:${fact.source_attempt_id}`, `fact:${fact.id}`); fact.evidence_refs.forEach((id) => connect(`artifact:${id}`, `fact:${fact.id}`)); });
  findings.forEach((finding) => finding.evidence_refs.forEach((id) => connect(`artifact:${id}`, `finding:${finding.id}`, 'finding')));
  return { nodes, edges };
}

function ProgressNode({ data }: NodeProps<Node<ProgressNodeData>>) { return <article className={`progress-node ${data.kind} ${data.status.toLowerCase()}`}><Handle type="target" position={Position.Left} /><div className="node-kicker"><span>{nodeLabel(data.kind)}</span><b>{statusLabel(data.status)}</b></div><p>{data.label}</p><small>{data.meta}</small><Handle type="source" position={Position.Right} /></article>; }
function Metric({ icon, label, value, accent = false }: { icon: React.ReactNode; label: string; value: number; accent?: boolean }) { return <article className={`metric ${accent ? 'accent' : ''}`}>{icon}<div><span>{label}</span><strong>{value}</strong></div></article>; }
function TargetControlPanel({ project, targets, manualUrl, setManualUrl, busy, onManual, onVerify, onConfirmPaid, onConfirm }: { project?: Project; targets: DiscoveredTarget[]; manualUrl: string; setManualUrl: (value: string) => void; busy: boolean; onManual: () => void; onVerify: () => void; onConfirmPaid: () => void; onConfirm: (targetId: string) => void }) {
  const state = project?.target_verification_status || 'UNVERIFIED';
  const attention = state === 'NEEDS_CONFIRMATION';
  const candidates = targets.filter((target) => ['CANDIDATE', 'PROVISIONING'].includes(target.status));
  return <section className={`target-control-panel ${attention ? 'attention' : ''}`} aria-label="靶机控制"><div className="target-control-heading"><div><span className="eyebrow">OPTIONAL TARGET</span><strong>靶机（可选）· {statusLabel(state)}</strong></div><div className="target-control-actions">{state === 'NEEDS_CONFIRMATION' && !candidates.length ? <button className="primary-button" disabled={busy} onClick={onConfirmPaid}><Check size={14} />确认启动</button> : null}<button className="icon-button" title="重新自动识别" aria-label="重新自动识别" disabled={busy} onClick={onVerify}><RefreshCw size={15} /></button></div></div><p>{project?.target_url ? <code>{project.target_url}</code> : project?.target_verification_reason || '未激活靶机，不影响题目与附件分析；需要网络交互时可再人工指定。'}</p>{candidates.length ? <div className="target-candidates">{candidates.map((target) => <article key={target.id}><div><b>{target.url}</b><small>{target.status} · {target.source || 'automatic'} · {Math.round((target.confidence || 0) * 100)}% · {target.probe_json?.summary || '等待探测'}</small></div><button className="primary-button" disabled={busy || target.status === 'PROVISIONING'} onClick={() => onConfirm(target.id)}><Check size={14} />确认</button></article>)}</div> : null}<div className="target-manual-row"><input value={manualUrl} onChange={(event) => setManualUrl(event.target.value)} placeholder="https://靶机地址:端口/" aria-label="手工指定靶机地址" /><button className="primary-button" disabled={busy || !manualUrl.trim()} onClick={onManual}><ShieldCheck size={15} />手工指定靶机</button></div></section>;
}
function IconAction({ icon, label, disabled, onClick, danger = false }: { icon: React.ReactNode; label: string; disabled: boolean; onClick: () => void; danger?: boolean }) { return <button className={`action-button ${danger ? 'danger' : ''}`} title={label} aria-label={label} disabled={disabled} onClick={onClick}>{icon}<span>{label}</span></button>; }
function ConcurrencyControl({ config, busy, onSave }: { config: ConcurrencyConfig; busy: boolean; onSave: (next: number) => void }) {
  const [draft, setDraft] = useState(String(config.max_agents));
  useEffect(() => { setDraft(String(config.max_agents)); }, [config.max_agents]);
  const commit = () => {
    const next = Number(draft);
    if (Number.isInteger(next) && next >= config.min && next <= config.max && next !== config.max_agents) onSave(next);
    else setDraft(String(config.max_agents));
  };
  return <label className="concurrency-control" title="全局并行 Solver Agent 上限，下次派发生效">最大并发 Agent<input type="number" min={config.min} max={config.max} step={1} disabled={busy} value={draft} onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); (event.target as HTMLInputElement).blur(); } }} /><small>{config.source === 'environment' ? '环境变量' : config.source === 'runtime' ? '运行时' : '默认'}</small></label>;
}
function externalAttachmentUrl(item: ExternalAttachment) { try { const parsed = new URL(item.url || ''); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : ''; } catch { return ''; } }
function externalAttachmentName(item: ExternalAttachment) { const url = externalAttachmentUrl(item); if (!url) return item.filename || '附件检查失败'; const parsed = new URL(url); const rawName = parsed.pathname.split('/').filter(Boolean).at(-1) || ''; let name = rawName; try { name = decodeURIComponent(rawName); } catch { /* Preserve malformed historical URL text. */ } return item.filename || (name && name !== 'view' ? name : parsed.hostname); }
function externalAttachmentStatus(item: ExternalAttachment) { if (item.reason) return item.reason; if (item.status === 'external_review_required') return '需要人工下载'; if (item.status === 'dynamic_attachment') return '需要从题目页获取'; return item.status.replaceAll('_', ' '); }
function NetworkProxyDialog({ busy, config, onSave, onClose }: { busy: boolean; config: NetworkProxyConfig; onSave: (config: NetworkProxyConfig) => void; onClose: () => void }) {
  const [draft, setDraft] = useState<NetworkProxyConfig>(config);
  const canSave = draft.mode !== 'custom' || /^https?:\/\/[^\s/]+/i.test(draft.proxy_url || '');
  return <div className="import-backdrop" role="dialog" aria-modal="true" aria-label="网络代理设置"><section className="import-dialog proxy-dialog"><header><div><span className="eyebrow">NETWORK EGRESS</span><h2>网络代理</h2></div><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header><div className="proxy-modes" role="group" aria-label="代理模式"><button className={draft.mode === 'direct' ? 'selected' : ''} onClick={() => setDraft({ ...draft, mode: 'direct' })}>直连</button><button className={draft.mode === 'system' ? 'selected' : ''} onClick={() => setDraft({ ...draft, mode: 'system' })}>系统代理</button><button className={draft.mode === 'custom' ? 'selected' : ''} onClick={() => setDraft({ ...draft, mode: 'custom' })}>自定义</button></div>{draft.mode === 'custom' ? <label>代理地址<input value={draft.proxy_url || ''} onChange={(event) => setDraft({ ...draft, proxy_url: event.target.value })} placeholder="http://127.0.0.1:7890" spellCheck={false} /></label> : null}<label>直连地址<textarea value={draft.no_proxy} onChange={(event) => setDraft({ ...draft, no_proxy: event.target.value })} placeholder="localhost,127.0.0.1,.internal" spellCheck={false} /></label><footer><span>{draft.mode === 'direct' ? 'DIRECT' : draft.mode === 'system' ? 'SYSTEM' : 'CUSTOM'}</span><button className="primary-button" disabled={busy || !canSave} onClick={() => onSave(draft)}><Check size={16} />保存</button></footer></section></div>;
}
function OpenVPNDialog({ busy, config, error, onSave, onCommand, onClear, onClose }: { busy: boolean; config: OpenVPNConfig; error: string; onSave: (input: { file: File; vaultPassword: string; routes: string; username: string; password: string }) => void; onCommand: (action: 'unlock' | 'lock' | 'connect' | 'disconnect', password?: string) => void; onClear: () => void; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [vaultPassword, setVaultPassword] = useState('');
  const [routes, setRoutes] = useState(config.routes.join('\n'));
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  useEffect(() => { setRoutes(config.routes.join('\n')); if (!config.locked) setVaultPassword(''); }, [config.routes.join('|'), config.locked]);
  const canSave = Boolean(file && vaultPassword.length >= 10 && routes.trim() && Boolean(username) === Boolean(password));
  const stateLabel = { unconfigured: '未配置', locked: '已锁定', unlocked: '已解锁', connected: '已连接', error: '连接异常' }[config.state];
  return <div className="import-backdrop" role="dialog" aria-modal="true" aria-label="OpenVPN 设置"><section className="import-dialog openvpn-dialog"><header><div><span className="eyebrow">ISOLATED VPN GATEWAY</span><h2>OpenVPN</h2></div><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header><div className={`openvpn-state ${config.state}`}><strong>{stateLabel}</strong><span>{config.connected ? 'Solver Worker 将通过隔离隧道访问指定网段' : '未连接时 Worker 保持原有网络路径'}</span></div>{error ? <div className="notice error">{error}</div> : null}{config.last_error ? <div className="openvpn-error">{config.last_error}</div> : null}{!config.configured || !config.locked ? <><label>OVPN 文件<input type="file" accept=".ovpn,text/plain" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label><label>VPN 转发网段<textarea value={routes} onChange={(event) => setRoutes(event.target.value)} placeholder={'10.10.0.0/16\n172.20.1.8'} spellCheck={false} /></label><div className="openvpn-grid"><label>VPN 用户名（可选）<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" /></label><label>VPN 密码（可选）<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" /></label></div></> : null}<label>{config.locked ? '输入主密码解锁' : config.configured ? '新配置的主密码' : '设置加密主密码'}<input type="password" value={vaultPassword} onChange={(event) => setVaultPassword(event.target.value)} minLength={10} autoComplete="new-password" placeholder="至少 10 个字符；服务重启后需重新输入" /></label>{config.routes.length ? <div className="openvpn-routes"><span>当前隧道路由</span><code>{config.routes.join(' · ')}</code></div> : null}<footer><span>ROUTE-NOPULL · FAIL CLOSED</span><div>{config.locked ? <button className="primary-button" disabled={busy || vaultPassword.length < 10} onClick={() => onCommand('unlock', vaultPassword)}><ShieldCheck size={15} />解锁</button> : null}{config.configured && !config.locked && !config.connected ? <button className="primary-button" disabled={busy} onClick={() => onCommand('connect')}><Play size={15} />连接</button> : null}{config.connected || config.desired_connected ? <button className="danger-button" disabled={busy} onClick={() => onCommand('disconnect')}><PauseCircle size={15} />断开</button> : null}{config.configured && !config.locked && !config.connected ? <button disabled={busy} onClick={() => onCommand('lock')}><X size={15} />锁定</button> : null}{!config.locked ? <button className="primary-button" disabled={busy || !canSave} onClick={() => file && onSave({ file, vaultPassword, routes, username, password })}><Check size={15} />保存配置</button> : null}{config.configured ? <button className="danger-button" disabled={busy || config.connected} onClick={onClear}><Trash2 size={15} />清除</button> : null}</div></footer></section></div>;
}
function TSecBenchDialog({ busy, config, testResult, error, onSave, onTest, onClose }: { busy: boolean; config: TSecBenchConfig; testResult: TSecBenchTestResult | null; error: string; onSave: (config: TSecBenchConfig & { token?: string; clear_token?: boolean }) => void; onTest: () => void; onClose: () => void }) {
  const [draft, setDraft] = useState<TSecBenchConfig>(config);
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  useEffect(() => { setDraft(config); if (config.token_configured) { setToken(''); setClearToken(false); } }, [config]);
  const canSave = /^https?:\/\/[^\s/]+/i.test(draft.base_url) && draft.timeout_seconds >= 1 && draft.timeout_seconds <= 300 && draft.max_concurrent >= 1 && draft.max_concurrent <= 3;
  const vpnLabel = testResult?.vpn.status === 'reachable' ? '可达' : testResult?.vpn.status === 'unreachable' ? '不可达' : '未验证';
  return <div className="import-backdrop" role="dialog" aria-modal="true" aria-label="TSecBench 设置"><section className="import-dialog tsecbench-dialog"><header><div><span className="eyebrow">BENCHMARK CONTROL PLANE</span><h2>TSecBench</h2></div><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header><div className="tsecbench-status"><div><span>认证</span><strong className={draft.token_configured ? 'ok' : 'idle'}>{draft.token_configured ? '已配置' : '未配置'}</strong></div><div><span>平台 API</span><strong className={testResult?.api.status === 'reachable' ? 'ok' : 'idle'}>{testResult?.api.status === 'reachable' ? `可达 · ${testResult.api.challenge_count} 题` : '未检测'}</strong></div><div><span>SSLVPN 路由</span><strong className={testResult?.vpn.status === 'reachable' ? 'ok' : testResult?.vpn.status === 'unreachable' ? 'error' : 'idle'}>{vpnLabel}</strong></div></div>{error ? <div className="notice error">{error}</div> : null}<label>Base URL<input value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} placeholder="https://tsecbench.zc.tencent.com" spellCheck={false} /></label><label>Benchmark Token<input type="password" value={token} disabled={clearToken} onChange={(event) => setToken(event.target.value)} placeholder={draft.token_configured ? '已配置，留空保持不变' : 'BENCHMARK_TOKEN'} autoComplete="new-password" spellCheck={false} /></label><div className="tsecbench-grid"><label>请求超时（秒）<input type="number" min={1} max={300} value={draft.timeout_seconds} onChange={(event) => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} /></label><label>并发实例上限<select value={draft.max_concurrent} onChange={(event) => setDraft({ ...draft, max_concurrent: Number(event.target.value) })}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option></select></label></div><label className="tsecbench-clear"><input type="checkbox" checked={clearToken} onChange={(event) => setClearToken(event.target.checked)} />清除当前 Token</label>{testResult ? <div className={`tsecbench-test ${testResult.vpn.status}`}><span>{testResult.progress.completed} 题完成 · {testResult.progress.correct_flags}/{testResult.progress.total_flags} Flag</span><small>{testResult.vpn.address || testResult.vpn.message}</small></div> : null}<footer><span>{draft.token_source.toUpperCase()} · TOKEN 不落库</span><div><button disabled={busy || !draft.token_configured} onClick={onTest}><RefreshCw size={15} />测试连接</button><button className="primary-button" disabled={busy || !canSave} onClick={() => onSave({ ...draft, token: token.trim() || undefined, clear_token: clearToken })}><Check size={16} />保存</button></div></footer></section></div>;
}
function SlabMatchDialog({ busy, config, testResult, error, onSave, onImport, onTest, onClose }: { busy: boolean; config: SlabMatchConfig; testResult: SlabMatchTestResult | null; error: string; onSave: (config: SlabMatchConfig & { access_key?: string; clear_access_key?: boolean }) => void; onImport: (config: SlabMatchConfig & { access_key?: string; clear_access_key?: boolean }) => void; onTest: () => void; onClose: () => void }) {
  const [draft, setDraft] = useState<SlabMatchConfig>(config);
  const [accessKey, setAccessKey] = useState('');
  const [clearAccessKey, setClearAccessKey] = useState(false);
  useEffect(() => { setDraft(config); if (config.access_key_configured) { setAccessKey(''); setClearAccessKey(false); } }, [config]);
  const canSave = /^https?:\/\/[^\s/]+/i.test(draft.base_url) && draft.timeout_seconds >= 1 && draft.timeout_seconds <= 300 && draft.max_concurrent >= 1 && draft.max_concurrent <= 10;
  const payload = { ...draft, access_key: accessKey.trim() || undefined, clear_access_key: clearAccessKey };
  const canUseConfiguredKey = draft.access_key_configured && !clearAccessKey;
  return <div className="import-backdrop" role="dialog" aria-modal="true" aria-label="Slab Match 设置"><section className="import-dialog tsecbench-dialog"><header><div><span className="eyebrow">CHALLENGE GROUP IMPORT</span><h2>Slab Match</h2></div><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header><div className="tsecbench-status"><div><span>认证</span><strong className={draft.access_key_configured ? 'ok' : 'idle'}>{draft.access_key_configured ? '已配置' : '未配置'}</strong></div><div><span>平台 API</span><strong className={testResult?.api.status === 'reachable' ? 'ok' : 'idle'}>{testResult?.api.status === 'reachable' ? `可达 · ${testResult.api.challenge_count} 题` : '未检测'}</strong></div><div><span>靶机检查</span><strong className="idle">按需</strong></div></div>{error ? <div className="notice error">{error}</div> : null}<label>Agent API Base URL<input value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} placeholder="https://example.com/slab-match/api/v1/agent" spellCheck={false} /></label><label>X-Agent-AccessKey<input type="password" value={accessKey} disabled={clearAccessKey} onChange={(event) => setAccessKey(event.target.value)} placeholder={draft.access_key_configured ? '已配置，留空保持不变' : 'Agent AccessKey'} autoComplete="new-password" spellCheck={false} /></label><div className="tsecbench-grid"><label>请求超时（秒）<input type="number" min={1} max={300} value={draft.timeout_seconds} onChange={(event) => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} /></label><label>Planner 动态靶机预算 N<input type="number" min={1} max={10} value={draft.max_concurrent} onChange={(event) => setDraft({ ...draft, max_concurrent: Number(event.target.value) })} /></label></div><p>这里只顺序导入题组和附件，不启动靶机或 Agent；并发调度由 Planner 处理。</p><label className="tsecbench-clear"><input type="checkbox" checked={clearAccessKey} onChange={(event) => setClearAccessKey(event.target.checked)} />清除当前 AccessKey</label>{testResult ? <div className="tsecbench-test unverified"><span>题目列表可读取</span><small>{testResult.endpoint.message}</small></div> : null}<footer><span>{draft.access_key_source.toUpperCase()} · ACCESSKEY 不落库</span><div><button disabled={busy || !draft.access_key_configured} onClick={onTest}><RefreshCw size={15} />测试连接</button><button disabled={busy || !canSave} onClick={() => onSave(payload)}><Check size={16} />仅保存</button><button className="primary-button" disabled={busy || !canSave || (!accessKey.trim() && !canUseConfiguredKey)} onClick={() => onImport(payload)}><FolderInput size={16} />保存并导入题组</button></div></footer></section></div>;
}
function ExternalAttachmentReview({ batchId, candidateId, items }: { batchId: string; candidateId: string; items: ExternalAttachment[] }) { if (!items.length) return null; return <div className="external-attachment-review" aria-label="外部附件人工审查">{items.map((item, index) => { const href = externalAttachmentUrl(item); const name = externalAttachmentName(item); const downloadUrl = `${API_BASE}/api/hands-free/imports/${batchId}/candidates/${candidateId}/external-attachments/${index}/download`; return <div className="external-attachment-row" key={`${item.url || item.filename || item.status}-${index}`}><AlertTriangle size={14} /><span><b>{name}</b><small>{externalAttachmentStatus(item)}</small></span>{href ? <div className="external-attachment-actions"><a href={downloadUrl} title={`通过 Aurora 下载 ${name}`} aria-label={`通过 Aurora 下载 ${name}`}><Download size={15} /></a><a href={href} target="_blank" rel="noopener noreferrer" title={`打开 ${name} 来源`} aria-label={`打开 ${name} 来源`}><ExternalLink size={15} /></a></div> : null}</div>; })}</div>; }
function HandsFreeDialog({ busy, url, setUrl, result, selected, setSelected, names, setNames, authMode, setAuthMode, cookie, setCookie, username, setUsername, password, setPassword, loginUrl, setLoginUrl, flagPrefixes, setFlagPrefixes, progress, progressStep, progressPercent, progressDetail, onScan, onContinue, onConfirm, onClose }: { busy: boolean; url: string; setUrl: (value: string) => void; result: ImportResult | null; selected: string[]; setSelected: (value: string[]) => void; names: Record<string, string>; setNames: (value: Record<string, string>) => void; authMode: 'anonymous' | 'cookie' | 'password'; setAuthMode: (value: 'anonymous' | 'cookie' | 'password') => void; cookie: string; setCookie: (value: string) => void; username: string; setUsername: (value: string) => void; password: string; setPassword: (value: string) => void; loginUrl: string; setLoginUrl: (value: string) => void; flagPrefixes: string; setFlagPrefixes: (value: string) => void; progress: ImportProgress; progressStep: number; progressPercent: number; progressDetail: string; onScan: () => void; onContinue: () => void; onConfirm: () => void; onClose: () => void }) {
  const toggle = (id: string) => setSelected(selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id]);
  const needsSession = result?.batch.status === 'NEEDS_SESSION';
  const diagnostics = result?.batch.diagnostics_json ?? [];
  const canAuthenticate = authMode === 'cookie' ? Boolean(cookie.trim()) : authMode === 'password' ? Boolean(username.trim() && password) : true;
  return <div className="import-backdrop" role="dialog" aria-modal="true" aria-label="解放双手"><section className="import-dialog"><header><div><span className="eyebrow">HANDS-FREE CATALOGER</span><h2>解放双手</h2></div><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header><p>归集 Agent 仅识别题目、链接和附件；登录凭据仅在本次抓取的隔离浏览器中使用。</p><div className={`import-progress ${progress}`} aria-live="polite"><div className="import-progress-copy"><strong>{progress === 'scanning' ? IMPORT_PROGRESS_STEPS[progressStep] : progress === 'ready' ? '导入识别完成' : progress === 'needs_session' ? '等待登录会话' : progress === 'failed' ? '导入失败' : '等待开始'}</strong><span>{progressDetail}</span><div className="import-progress-track" role="progressbar" aria-label="导入进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressPercent}><i style={{ width: `${progressPercent}%` }} /></div></div><b className="import-progress-percent">{progressPercent}%</b><ol>{IMPORT_PROGRESS_STEPS.map((step, index) => <li key={step} className={index < progressStep || progress === 'ready' ? 'complete' : progress === 'scanning' && index === progressStep ? 'active' : ''} title={step}>{index + 1}</li>)}</ol></div>{!result || needsSession ? <><div className="import-url"><input value={url} disabled={Boolean(needsSession)} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.org/ctf/tasks" aria-label="题目列表网址" /><button className="primary-button" disabled={busy || !url.trim() || !canAuthenticate} onClick={needsSession ? onContinue : onScan}><FileSearch size={16} />{needsSession ? '继续抓取' : '识别'}</button></div><div className="auth-panel"><label>访问方式</label><div className="auth-modes"><button className={authMode === 'anonymous' ? 'selected' : ''} onClick={() => setAuthMode('anonymous')}>匿名</button><button className={authMode === 'cookie' ? 'selected' : ''} onClick={() => setAuthMode('cookie')}>Cookie</button><button className={authMode === 'password' ? 'selected' : ''} onClick={() => setAuthMode('password')}>账号密码</button></div>{authMode === 'cookie' ? <textarea value={cookie} onChange={(event) => setCookie(event.target.value)} placeholder="粘贴已登录浏览器的 Cookie Header" aria-label="Cookie" /> : null}{authMode === 'password' ? <div className="auth-fields"><input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="账号或邮箱" aria-label="账号" /><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="密码" aria-label="密码" /><input value={loginUrl} onChange={(event) => setLoginUrl(event.target.value)} placeholder="登录页 URL（可选）" aria-label="登录页 URL" /></div> : null}</div>{needsSession ? <div className="import-auth-notice"><strong>需要登录会话</strong><span>{result?.batch.auth_message || '请提供已登录 Cookie 后继续。'}{result?.batch.login_domain ? ` (${result.batch.login_domain})` : ''}</span></div> : null}</> : null}{result && !needsSession ? <><div className="import-summary"><span>{result.batch.title || result.batch.source_url}</span><small>{result.batch.platform || 'generic'} · {result.batch.extraction_strategy || 'unknown'} · {result.batch.pages_scanned || 1} 页 · {result.candidates.length} 题</small></div><label className="import-flag-prefixes">Flag 前缀（逗号分隔，可多个）<input value={flagPrefixes} onChange={(event) => setFlagPrefixes(event.target.value)} placeholder="flag,DASCTF" aria-label="Flag 前缀" spellCheck={false} /><small>大小写不敏感；留空使用全局默认 flag，提交仍按解出的原始格式。</small></label>{diagnostics.length ? <section className="import-diagnostics">{diagnostics.slice(0, 6).map((item, index) => <p key={`${item.code}-${index}`}><AlertTriangle size={14} /><span>{item.message || item.code}</span></p>)}</section> : null}{result.candidates.length ? <><div className="candidate-list">{result.candidates.map((candidate) => <article className="import-candidate" key={candidate.id}><label><input type="checkbox" checked={selected.includes(candidate.id)} onChange={() => toggle(candidate.id)} /><span>{candidate.challenge_type || 'unknown'} · {Math.round(candidate.confidence * 100)}% · {String(candidate.source_metadata_json?.provenance || 'verified')}</span></label><input value={names[candidate.id] ?? candidate.title} onChange={(event) => setNames({ ...names, [candidate.id]: event.target.value })} aria-label="项目名称" /><p>{candidate.description || candidate.challenge_url}</p><small>{candidate.staged_attachments_json.length} 个附件已暂存 · {candidate.external_attachments_json.length} 个外部附件待人工处理</small><ExternalAttachmentReview batchId={result.batch.id} candidateId={candidate.id} items={candidate.external_attachments_json} /></article>)}</div><footer><span>{selected.length} 个项目待创建</span><button className="primary-button" disabled={busy || !selected.length} onClick={onConfirm}><Plus size={16} />批量创建项目</button></footer></> : <div className="import-empty"><FolderInput size={24} />未发现可验证的题目；请检查诊断信息或提供有效登录会话。</div>}</> : !needsSession ? <div className="import-empty"><FolderInput size={24} />输入赛事、题库或题目列表地址后开始归集。</div> : null}</section></div>;
}
function Tab({ icon, label, active, onClick }: { icon: React.ReactNode; label: string; active: boolean; onClick: () => void }) { return <button className={active ? 'active' : ''} onClick={onClick} title={label}>{icon}<span>{label}</span></button>; }

function InspectPanel({ selected, onClear, board, events, latestTrace, runtimeLogs }: { selected?: ProgressNodeData; onClear: () => void; board: Blackboard | null; events: WorkerEvent[]; latestTrace?: LLMTrace; runtimeLogs: RuntimeLogs | null }) {
  const relatedEvents = selected ? events.filter((event) => event.worker_id === selected.entityId || event.attempt_id === selected.entityId).slice(0, 8) : events.slice(0, 8);
  const reflection = selected?.kind === 'checkpoint' ? board?.checkpoints.find((item) => item.id === selected.entityId) : undefined;
  const generatedIntents = reflection ? reflection.generated_intent_ids.map((id) => board?.intents.find((intent) => intent.id === id)).filter((intent): intent is Intent => Boolean(intent)) : [];
  return <section className="drawer-content"><div className="drawer-title"><span>检查器</span>{selected ? <button className="icon-button" title="清除节点选择" aria-label="清除节点选择" onClick={onClear}><X size={15} /></button> : null}</div>{selected ? <><div className={`detail-status ${selected.kind}`}><span>{nodeLabel(selected.kind)}</span><b>{statusLabel(selected.status)}</b></div><h2>{selected.label}</h2><p className="detail-meta">{selected.meta}</p></> : <><div className="detail-status idle"><span>项目概览</span><b>{statusLabel(board?.project.status ?? 'IDLE')}</b></div><h2>{board?.project.name ?? '等待项目'}</h2><p className="detail-meta">在进度图中选择节点查看实体关系与执行详情。</p></>}{reflection ? <section className="detail-section"><label>反思结果</label>{reflection.conclusions.map((item) => <p className="decision" key={`conclusion-${item}`}>结论 · {item}</p>)}{reflection.hypotheses.map((item) => <p className="muted" key={`hypothesis-${item}`}>假设 · {item}</p>)}{reflection.failed_routes.map((item) => <p className="muted" key={`failed-${item}`}>失败路线 · {item}</p>)}{reflection.next_steps.map((item) => <p className="muted" key={`next-${item}`}>下一步 · {item}</p>)}{generatedIntents.map((intent) => <p className="decision" key={intent.id}>Intent · {intent.objective}</p>)}</section> : null}<section className="detail-section"><label>最近关联事件</label>{relatedEvents.length ? relatedEvents.map((event) => <article className="event-row" key={event.id}><b>{eventLabel(event.event_type)}</b><small>{new Date(event.created_at).toLocaleTimeString()}</small><p>{JSON.stringify(event.payload_json).slice(0, 120)}</p></article>) : <p className="muted">暂无关联事件。</p>}</section><section className="detail-section"><label>最新模型决策</label><p className="decision">{latestTrace ? String(latestTrace.decision_summary.reason_summary ?? JSON.stringify(latestTrace.decision_summary)) : '尚未执行模型回合。'}</p></section><section className="detail-section"><label>运行容器</label><p className="muted">{runtimeLogs?.containers.length ? `${runtimeLogs.containers.length} 个容器正在或曾被记录。` : '暂无活动容器。'}</p></section></section>;
}
function ControlPanel(props: { busy: boolean; locked: boolean; intentObjective: string; setIntentObjective: (v: string) => void; intentTool: string; setIntentTool: (v: string) => void; intentRequest: string; setIntentRequest: (v: string) => void; toolName: string; setToolName: (v: string) => void; toolRequest: string; setToolRequest: (v: string) => void; browserSourceUrl: string; setBrowserSourceUrl: (v: string) => void; browserCookie: string; setBrowserCookie: (v: string) => void; onBrowserSession: () => void; onIntent: () => void; onTool: () => void }) { return <section className="drawer-content control-panel"><div className="drawer-title"><span>控制台</span></div><label>题目站浏览器会话</label><input value={props.browserSourceUrl} onChange={(event) => props.setBrowserSourceUrl(event.target.value)} placeholder="题目详情页 URL" /><textarea value={props.browserCookie} onChange={(event) => props.setBrowserCookie(event.target.value)} placeholder="Cookie Header" /><button disabled={props.busy || !props.browserSourceUrl.trim() || !props.browserCookie.trim()} onClick={props.onBrowserSession}><ShieldCheck size={16} />更新浏览器会话</button><label>创建意图</label><textarea value={props.intentObjective} onChange={(event) => props.setIntentObjective(event.target.value)} /><select value={props.intentTool} onChange={(event) => props.setIntentTool(event.target.value)}>{toolOptions.map((tool) => <option key={tool}>{tool}</option>)}</select><textarea value={props.intentRequest} onChange={(event) => props.setIntentRequest(event.target.value)} /><button className="primary-button" disabled={props.busy || props.locked} onClick={props.onIntent}><GitBranch size={16} />加入执行图</button><label>手动工具</label><select value={props.toolName} onChange={(event) => props.setToolName(event.target.value)}>{toolOptions.map((tool) => <option key={tool}>{tool}</option>)}</select><textarea value={props.toolRequest} onChange={(event) => props.setToolRequest(event.target.value)} /><button disabled={props.busy} onClick={props.onTool}><TerminalSquare size={16} />执行</button></section>; }
function LiveActionStrip({ events, onSelect }: { events: WorkerEvent[]; onSelect: (event: WorkerEvent) => void }) {
  const actions = events.filter(isLiveAction).slice(0, 12);
  return <section className="live-action-strip" aria-live="polite"><div className="live-action-heading"><span>模型实时动作</span><small>实时流 · {actions.length ? '持续更新' : '等待模型或工具动作'}</small></div><div className="live-action-list">{actions.length ? actions.map((event) => <button key={event.id} className={`live-action ${actionStatus(event)}`} onClick={() => onSelect(event)}><span className="action-dot" /><span><b>{eventLabel(event.event_type)}</b><small>{eventSummary(event)} · {new Date(event.created_at).toLocaleTimeString()}</small></span></button>) : <p className="muted">尚无可展示的模型决策或执行动作。</p>}</div></section>;
}

function ConclusionEvidencePanel({ board, preview, onPreview, evidenceDrafts, activeEvidenceIndex, onSelectEvidence, onDescription, onToggleArtifact, onAddEvidence, onRemoveEvidence, conclusion, setConclusion, confidence, setConfidence, category, setCategory, onDerive, disabled }: { board: Blackboard | null; preview: string; onPreview: (id: string) => void; evidenceDrafts: EvidenceDraft[]; activeEvidenceIndex: number; onSelectEvidence: (index: number) => void; onDescription: (index: number, value: string) => void; onToggleArtifact: (index: number, artifactId: string) => void; onAddEvidence: () => void; onRemoveEvidence: (index: number) => void; conclusion: string; setConclusion: (value: string) => void; confidence: number; setConfidence: (value: number) => void; category: string; setCategory: (value: string) => void; onDerive: () => void; disabled: boolean }) {
  const artifacts = new Map((board?.artifacts ?? []).map((artifact) => [artifact.id, artifact]));
  const facts = (board?.facts ?? []).filter((fact) => fact.status === 'ACTIVE').slice().reverse();
  const activeDraft = evidenceDrafts[activeEvidenceIndex] ?? evidenceDrafts[0];
  const canSubmit = Boolean(conclusion.trim()) && evidenceDrafts.length > 0 && evidenceDrafts.every((item) => item.description.trim() && item.artifact_refs.length);
  return <section className="drawer-content"><div className="drawer-title"><span>结论证据</span></div><section className="conclusion-evidence-list">{facts.length ? facts.map((fact) => { const evidenceItems = fact.evidence_items ?? []; return <article className="conclusion-evidence-card" key={fact.id}><div className="conclusion-evidence-header"><span>{fact.category || 'general'}</span><b>{Math.round(fact.confidence * 100)}%</b></div><p>{fact.statement}</p><small>{new Date(fact.created_at).toLocaleString()}{fact.source_attempt_id ? ` · ${shortId(fact.source_attempt_id)}` : ''}</small>{evidenceItems.length ? <div className="semantic-evidence-list">{evidenceItems.map((item, index) => <div className="semantic-evidence-item" key={`${fact.id}-${index}`}><div><CircleDot size={13} /><p>{item.description}</p></div><div className="semantic-evidence-sources">{item.artifact_refs.map((id) => { const artifact = artifacts.get(id); return artifact ? <button key={id} title="查看原始证据" onClick={() => onPreview(id)}><FileSearch size={13} /><span>{artifact.summary || artifact.type}</span></button> : <span className="missing-evidence" key={id}>证据 {shortId(id)} 不可用</span>; })}</div></div>)}</div> : <div className="legacy-evidence"><label>仅关联原始材料</label><div className="semantic-evidence-sources">{fact.evidence_refs.length ? fact.evidence_refs.map((id) => { const artifact = artifacts.get(id); return artifact ? <button key={id} title="查看原始证据" onClick={() => onPreview(id)}><FileSearch size={13} /><span>{artifact.summary || artifact.type}</span></button> : <span className="missing-evidence" key={id}>证据 {shortId(id)} 不可用</span>; }) : <span className="missing-evidence">未关联可验证证据</span>}</div></div>}</article>; }) : <p className="muted">暂无已形成的结论。</p>}</section><div className="conclusion-form"><div className="evidence-builder-heading"><label>证据</label><button type="button" title="添加证据" disabled={disabled || evidenceDrafts.length >= 10} onClick={onAddEvidence}><Plus size={14} />添加证据</button></div><div className="evidence-drafts">{evidenceDrafts.map((item, index) => { const valid = Boolean(item.description.trim() && item.artifact_refs.length); return <article className={`${index === activeEvidenceIndex ? 'active' : ''} ${valid ? 'valid' : 'invalid'}`} key={index}><div className="evidence-draft-header"><button type="button" className="evidence-draft-select" onClick={() => onSelectEvidence(index)}><CircleDot size={14} /><span>证据 {index + 1}</span><small>{item.artifact_refs.length} 份原始材料</small></button><button type="button" className="icon-button" title="删除证据" aria-label={`删除证据 ${index + 1}`} disabled={disabled || evidenceDrafts.length === 1} onClick={() => onRemoveEvidence(index)}><Trash2 size={14} /></button></div><textarea value={item.description} aria-label={`证据 ${index + 1} 描述`} aria-invalid={!item.description.trim()} disabled={disabled} onFocus={() => onSelectEvidence(index)} onChange={(event) => onDescription(index, event.target.value)} placeholder="例如：参数 id=1' 返回 SQL 语法错误" /></article>; })}</div><label>证据 {activeEvidenceIndex + 1} 的原始材料</label><div className="evidence-list evidence-source-picker">{board?.artifacts.slice().reverse().map((artifact) => <article key={artifact.id} className={activeDraft?.artifact_refs.includes(artifact.id) ? 'selected' : ''}><label><input type="checkbox" checked={activeDraft?.artifact_refs.includes(artifact.id) ?? false} disabled={disabled || !activeDraft} onChange={() => onToggleArtifact(activeEvidenceIndex, artifact.id)} /><span><b>{artifact.type}</b><small>{artifact.summary || artifact.id}</small></span></label><button title="查看原始证据" aria-label="查看原始证据" onClick={() => onPreview(artifact.id)}><FileSearch size={15} /></button></article>)}</div><label>结论</label><textarea value={conclusion} onChange={(event) => setConclusion(event.target.value)} disabled={disabled} placeholder="例如：目标接口的 id 参数存在 SQL 注入" /><label>置信度 {Math.round(confidence * 100)}%</label><input type="range" min="0" max="1" step="0.05" value={confidence} disabled={disabled} onChange={(event) => setConfidence(Number(event.target.value))} /><input value={category} onChange={(event) => setCategory(event.target.value)} disabled={disabled} aria-label="结论分类" placeholder="分类" /><button className="primary-button" disabled={disabled || !canSubmit} onClick={onDerive}><BrainCircuit size={16} />形成结论</button></div>{preview ? <pre>{preview}</pre> : null}</section>;
}

function isLiveAction(event: WorkerEvent) { return ['manager.decision', 'reason.started', 'reason.completed', 'reason.fallback', 'multi_agent.batch_started', 'multi_agent.batch_completed', 'worker.started', 'worker.heartbeat', 'worker.completed', 'worker.timed_out', 'worker.reconciled', 'context.built', 'codex.session_started', 'codex.progress', 'codex.resume_scheduled', 'codex.resume_rejected', 'blackboard.fact_appended', 'checkpoint.saved', 'checkpoint.failed', 'attempt.soft_deadline', 'attempt.budget_enforced', 'attempt.finalization_started', 'attempt.resume_manifest_saved', 'llm.completed', 'tool.started', 'tool.executed', 'attempt.completed', 'attempt.timed_out', 'reflection.completed', 'reflection.fallback', 'subagent.completed', 'observer.decision', 'project.completed', 'target.verification.completed', 'target.probe_completed', 'target.probe_failed'].includes(event.event_type); }
function actionStatus(event: WorkerEvent) { if (event.payload_json.success === false || event.payload_json.status === 'FAILED' || event.payload_json.status === 'TIMEOUT' || event.event_type === 'target.probe_failed') return 'failed'; if (event.event_type === 'worker.started' || event.event_type === 'context.built' || event.event_type === 'tool.started' || event.event_type === 'codex.progress' || event.event_type === 'codex.session_started') return 'running'; return 'completed'; }
function eventSummary(event: WorkerEvent) { const payload = event.payload_json; return String(payload.activity_label ?? payload.summary ?? payload.reason ?? payload.tool_name ?? payload.decision ?? payload.model ?? payload.status ?? '已记录'); }
function graphEntityExists(id: string, board: Blackboard) { const [, entityId] = id.split(':'); return [...board.intents, ...board.workers, ...board.attempts, ...board.checkpoints, ...board.facts, ...board.artifacts, ...board.findings].some((item) => item.id === entityId); }
function shortId(id: string) { return id.length > 12 ? id.slice(-10) : id; }
function formatBytes(size: number) { return size > 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : size > 1024 ? `${Math.round(size / 1024)} KB` : `${size} B`; }
function nodeLabel(kind: ProgressNodeData['kind']) { return ({ intent: '意图', worker: 'Worker', attempt: '尝试', activity: '实时动作', checkpoint: '轮次反思', hypothesis: '假设', fact: '事实', artifact: '证据', finding: '发现' })[kind]; }
function statusLabel(status: string) { return ({ ACTIVE: '进行中', WORKING: '重新解题中', FLAG_READY: '候选待确认', LOCAL_VERIFIED: '本地验证通过', AWAITING_MANUAL_VALIDATION: '等待人工确认', WAITING_RESOURCE: '等待资源', SUBMITTED: '已提交', ACCEPTED: '已接受', REJECTED: '已拒绝', COMPLETED: '完成', FAILED: '失败', CANCELLED: '取消', PENDING: '待执行', CLAIMED: '已领取', RUNNING: '运行中', FINALIZING: '收尾中', CONCLUDING: '提交终态中', PARTIAL: '部分完成', TIMEOUT: '超时', BLOCKED: '阻塞', INTERRUPTED: '中断', EVIDENCE: '证据', FACT: '事实', HYPOTHESIS: '假设', FINDING: '发现', IDLE: '空闲' } as Record<string, string>)[status] ?? status; }
function eventLabel(type: string) { return ({ 'hint.created': '线索已注入', 'manager.decision': '规划决策', 'reason.started': 'Reason Agent 启动', 'reason.completed': 'Reason Agent 完成', 'reason.fallback': 'Reason Agent 降级', 'multi_agent.batch_started': '并行探索启动', 'multi_agent.batch_completed': '并行探索完成', 'worker.started': 'Worker 启动', 'worker.completed': 'Worker 已结束', 'worker.timed_out': 'Worker 超时回收', 'worker.reconciled': 'Worker 状态已协调', 'context.built': '上下文构建', 'codex.session_started': 'Codex 会话已启动', 'codex.progress': 'Codex 正在推进', 'codex.resume_scheduled': 'Codex 会话等待恢复', 'codex.resume_rejected': '恢复校验未通过', 'blackboard.fact_appended': '实时事实已写入', 'checkpoint.saved': '实时检查点已保存', 'checkpoint.failed': '检查点保存失败', 'attempt.soft_deadline': '达到软截止', 'attempt.budget_enforced': '预算已强制执行', 'attempt.finalization_started': '开始结构化收尾', 'attempt.resume_manifest_saved': '恢复清单已保存', 'llm.completed': '模型回合完成', 'tool.started': '开始执行工具', 'tool.executed': '工具执行', 'attempt.completed': '尝试完成', 'attempt.timed_out': '尝试超时回收', 'subagent.completed': '子代理完成', 'fact.created': '事实创建', 'fact.merged': '事实合并', 'intent.created': '意图创建', 'observer.decision': '观察器决策', 'project.completed': '项目完成', 'target.verification.completed': '靶机校验', 'target.probe_completed': '靶机网络可达', 'target.probe_failed': '靶机网络诊断失败' } as Record<string, string>)[type] ?? type; }

createRoot(document.getElementById('root')!).render(<App />);
