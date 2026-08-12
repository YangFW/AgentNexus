const PREFERENCE_PREFIX = 'agentnexus-';
const LEGACY_PREFERENCE_PREFIXES = ['zhishu-'];

function readPreference(name) {
  const value = localStorage.getItem(`${PREFERENCE_PREFIX}${name}`);
  if (value !== null) return value;
  for (const prefix of LEGACY_PREFERENCE_PREFIXES) {
    const legacy = localStorage.getItem(`${prefix}${name}`);
    if (legacy !== null) {
      localStorage.setItem(`${PREFERENCE_PREFIX}${name}`, legacy);
      return legacy;
    }
  }
  return null;
}

function writePreference(name, value) {
  localStorage.setItem(`${PREFERENCE_PREFIX}${name}`, String(value));
}

const state = {
  skills: [],
  mcp: [],
  agents: [],
  tasks: [],
  models: [],
  loops: [],
  memories: [],
  conversationSummaries: [],
  artifacts: [],
  expertTemplates: [],
  expertInstallations: [],
  expertTeams: [],
  expertTeamRuns: [],
  loopTriggerEvents: [],
  loopNotifications: [],
  loopEditorDirty: false,
  loopStateDirty: false,
  capabilities: {},
  uploads: [],
  selectedSkill: null,
  selectedSkillFile: null,
  skillFiles: [],
  selectedMcp: null,
  selectedAgent: null,
  selectedModel: null,
  selectedLoop: null,
  selectedMemory: null,
  selectedConversationSummary: null,
  selectedArtifact: null,
  selectedExpertTemplate: null,
  selectedExpertTeam: null,
  selectedExpertRun: null,
  workbenchMode: readPreference('workbench-mode') === 'expert' ? 'expert' : 'agent',
  currentExpertSelection: null,
  expertRunPollTimer: null,
  expertRunPollToken: 0,
  loopPollTimer: null,
  currentTask: null,
  taskRuntime: null,
  runtimeTaskId: null,
  runtimeTimer: null,
  eventSource: null,
  streamTaskId: null,
  streamCursor: 0,
  streamGeneration: 0,
  streamRetryTimer: null,
  streamRetryCount: 0,
  seenEventIds: new Set(),
  conversationId: readPreference('conversation') || createConversationId(),
};

const $ = (id) => document.getElementById(id);

function createConversationId() {
  const value = globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `conv_${value.slice(0, 24)}`;
}

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const res = await fetch(path, {
    headers: { ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText;
    try {
      const parsed = JSON.parse(text);
      message = parsed.detail || parsed.message || message;
    } catch (_) {}
    const error = new Error(message);
    error.status = res.status;
    error.path = path;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]));
}

function agentIconSvg(agent = {}) {
  const id = String(agent.id || agent.agent_id || '');
  const name = String(agent.name || agent.agent_name || '');
  if (id === 'general-agent') {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3.5 7.2 4.2v8.6L12 20.5l-7.2-4.2V7.7Z"/><circle cx="12" cy="12" r="2.7"/><path d="M12 9.3V6.5M9.7 13.4l-2.5 1.5M14.3 13.4l2.5 1.5"/></svg>';
  }
  if (/expert|专家/.test(`${id} ${name}`)) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="8" r="2.7"/><circle cx="17" cy="9" r="2.3"/><path d="M3.8 18.5c.5-2.9 1.9-4.5 4.2-4.5s3.7 1.6 4.2 4.5M13.3 18c.4-2.3 1.6-3.7 3.7-3.7s3.3 1.4 3.7 3.7"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 4 1.8 4.2L18 10l-4.2 1.8L12 16l-1.8-4.2L6 10l4.2-1.8Z"/><path d="m18.5 15 .8 1.8 1.7.7-1.7.8-.8 1.7-.7-1.7-1.8-.8 1.8-.7Z"/></svg>';
}

function agentAvatarTone(agent = {}) {
  const id = String(agent.id || agent.agent_id || '');
  const name = String(agent.name || agent.agent_name || '');
  if (id === 'general-agent') return 'core';
  if (/expert|专家/.test(`${id} ${name}`)) return 'expert';
  return 'custom';
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

let toastTimer;
function notify(message, type = 'success') {
  const toast = $('toast');
  toast.textContent = message;
  toast.className = `toast ${type} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}

function setBusy(button, busy, label = '保存中…') {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.label;
}

function switchTab(tab) {
  document.querySelectorAll('.nav').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach((el) => el.classList.remove('active'));
  $(`tab-${tab}`).classList.add('active');
  if (tab !== 'loops') {
    clearTimeout(state.loopPollTimer);
    state.loopPollTimer = null;
  }
  if (tab === 'skills') {
    loadSkillsOnly().catch((err) => notify(`技能刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'memory') {
    loadMemoriesOnly({ preserveSelection: true }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'artifacts') {
    loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'loops') {
    loadLoopsOnly().catch((err) => notify(`自动化刷新失败：${err.message || err}`, 'error'));
  }
}

async function loadSkillsOnly() {
  state.skills = await api('/api/skills');
  renderSkills();
  return state.skills;
}

async function loadAll() {
  const [skills, mcp, agents, tasks, models, loops, capabilities] = await Promise.all([
    api('/api/skills'),
    api('/api/mcp'),
    api('/api/agents'),
    api('/api/tasks'),
    api('/api/models'),
    api('/api/loops'),
    api('/api/capabilities'),
  ]);
  state.skills = skills;
  state.mcp = mcp;
  state.agents = agents;
  state.tasks = tasks;
  state.models = models;
  state.loops = loops;
  state.capabilities = capabilities;
  renderAgentsSelect();
  renderTaskModelSelect();
  renderSkills();
  renderMcp();
  renderAgents();
  renderTasks();
  renderModels();
  renderLoops();
  renderCapabilities();
  const [memoryLoad, artifactLoad, expertLoad] = await Promise.allSettled([
    loadMemoriesOnly({ preserveSelection: true }),
    loadArtifactsOnly({ preserveSelection: true }),
    loadExpertWorkspace({ preserveSelection: true }),
  ]);
  if (memoryLoad.status === 'rejected') {
    console.warn('记忆模块初始化失败', memoryLoad.reason);
    $('memoryEffectiveMeta').textContent = '记忆服务暂不可用，可稍后重新计算';
    $('memoryEffectiveContext').textContent = '平台其他功能仍可正常使用。';
  }
  if (artifactLoad.status === 'rejected') {
    console.warn('产物工作区初始化失败', artifactLoad.reason);
    state.artifacts = [];
    state.selectedArtifact = null;
    renderArtifactWorkspace();
    resetArtifactPreview('产物服务暂不可用，请稍后刷新。');
  }
  if (expertLoad.status === 'rejected') {
    console.warn('专家团模块初始化失败', expertLoad.reason);
    state.expertTemplates = [];
    state.expertInstallations = [];
    state.expertTeams = [];
    state.expertTeamRuns = [];
    $('expertTemplateList').innerHTML = '<div class="meta empty">专家模板服务暂不可用，请稍后刷新。</div>';
    $('expertInstallationList').innerHTML = '<div class="meta empty">暂时无法读取已安装专家。</div>';
    $('expertTeamList').innerHTML = '<div class="meta empty">专家团服务暂不可用，请稍后刷新。</div>';
    resetExpertRunLive('暂时无法读取团队运行。');
  }
}

function renderAgentsSelect() {
  const select = $('agentSelect');
  const previous = select.value || readPreference('agent') || 'general-agent';
  select.innerHTML = state.agents.map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}</option>`).join('');
  if (state.agents.some((a) => a.id === previous)) select.value = previous;
  else if (state.agents.some((a) => a.id === 'general-agent')) select.value = 'general-agent';
}

function enabledWorkbenchTeams() {
  return state.expertTeams.filter((team) => team.enabled);
}

function renderWorkbenchTeamOptions() {
  const select = $('expertTeamSelect');
  if (!select) return;
  const teams = enabledWorkbenchTeams();
  const previous = select.value || readPreference('expert-team') || '';
  select.innerHTML = `<option value="">自动匹配专家团${teams.length ? '' : '（暂无可用团队）'}</option>${teams.map((team) => `<option value="${escapeHtml(team.id)}">指定：${escapeHtml(team.name)}</option>`).join('')}`;
  if (teams.some((team) => team.id === previous)) select.value = previous;
  else select.value = '';
}

function renderWorkbenchMode() {
  const expert = state.workbenchMode === 'expert';
  document.querySelectorAll('[data-workbench-mode]').forEach((button) => {
    const active = button.dataset.workbenchMode === state.workbenchMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  $('agentSelect')?.classList.toggle('hidden', expert);
  $('expertTeamControl')?.classList.toggle('hidden', !expert);
  const selectedTeam = enabledWorkbenchTeams().find((team) => team.id === $('expertTeamSelect')?.value);
  if ($('workbenchModeDescription')) {
    $('workbenchModeDescription').textContent = expert
      ? '自动匹配已配置的专家团；团队内成员独立并行分析，再由主管统一汇总和验收。'
      : '描述目标或上传资料，任务助手会自动选择合适的技能和工具。';
  }
  if ($('composerModeHint')) {
    $('composerModeHint').textContent = expert
      ? '专家协作 · 根据目标匹配已启用团队'
      : '单智能体处理 · 自动匹配技能和工具';
  }
  if ($('messageInput')) {
    $('messageInput').placeholder = expert
      ? '描述需要多位专家协作分析的目标，或上传相关资料…'
      : '输入任务，例如：根据我上传的资料生成一份项目分析报告…';
  }
  if ($('expertTeamHint')) {
    $('expertTeamHint').textContent = selectedTeam
      ? `固定使用“${selectedTeam.name}”，由团队主管汇总`
      : `根据任务目标自动匹配 · 当前 ${enabledWorkbenchTeams().length} 个可用团队`;
  }
}

function setWorkbenchMode(mode, { persist = true } = {}) {
  state.workbenchMode = mode === 'expert' ? 'expert' : 'agent';
  if (persist) writePreference('workbench-mode', state.workbenchMode);
  renderWorkbenchMode();
}

function renderTaskModelSelect() {
  const select = $('taskModelSelect');
  const enabled = state.models.filter((m) => m.enabled);
  const preferred = select.value || readPreference('model') || enabled.find((m) => m.id !== 'deterministic')?.id || 'deterministic';
  select.innerHTML = enabled.map((m) => `<option value="${escapeHtml(m.id)}">模型：${escapeHtml(m.name)}</option>`).join('');
  if (enabled.some((m) => m.id === preferred)) select.value = preferred;
}

function addMessage(role, content, eventId = null) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  if (eventId != null) div.dataset.eventId = String(eventId);
  renderMessageContent(div, content);
  $('conversation').appendChild(div);
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function renderMessageContent(element, content) {
  if (element.classList.contains('user')) {
    element.textContent = content;
    return;
  }
  element.innerHTML = renderMarkdown(content);
}

function renderMarkdown(content) {
  const lines = escapeHtml(String(content ?? '').replace(/\r\n?/g, '\n')).split('\n');
  const output = [];
  let paragraph = [];
  let listType = '';
  let code = null;

  const inline = (value) => value
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((\/api\/artifacts\/[^)\s]+|https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  const closeParagraph = () => {
    if (paragraph.length) output.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (listType) output.push(`</${listType}>`);
    listType = '';
  };
  const openList = (type) => {
    closeParagraph();
    if (listType !== type) {
      closeList();
      output.push(`<${type}>`);
      listType = type;
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = line.match(/^```([a-zA-Z0-9_-]*)\s*$/);
    if (fence) {
      closeParagraph(); closeList();
      if (code) {
        output.push(`<pre><code${code.language ? ` class="language-${code.language}"` : ''}>${code.lines.join('\n')}</code></pre>`);
        code = null;
      } else {
        code = { language: fence[1], lines: [] };
      }
      continue;
    }
    if (code) { code.lines.push(line); continue; }
    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const next = lines[index + 1] || '';
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(next)) {
      closeParagraph(); closeList();
      const headers = line.replace(/^\||\|$/g, '').split('|').map((cell) => inline(cell.trim()));
      output.push('<div class="md-table-wrap"><table><thead><tr>' + headers.map((cell) => `<th>${cell}</th>`).join('') + '</tr></thead><tbody>');
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const cells = lines[index].replace(/^\||\|$/g, '').split('|').map((cell) => inline(cell.trim()));
        output.push('<tr>' + cells.map((cell) => `<td>${cell}</td>`).join('') + '</tr>');
        index += 1;
      }
      output.push('</tbody></table></div>');
      index -= 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      closeParagraph(); closeList(); output.push('<hr>'); continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) { openList('ul'); output.push(`<li>${inline(unordered[1])}</li>`); continue; }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) { openList('ol'); output.push(`<li>${inline(ordered[1])}</li>`); continue; }
    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) { closeParagraph(); closeList(); output.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }
    closeList();
    paragraph.push(line);
  }
  if (code) output.push(`<pre><code${code.language ? ` class="language-${code.language}"` : ''}>${code.lines.join('\n')}</code></pre>`);
  closeParagraph(); closeList();
  return output.join('');
}

async function sendTask() {
  const message = $('messageInput').value.trim();
  if (!message) return;
  const expertMode = state.workbenchMode === 'expert';
  if (expertMode && !enabledWorkbenchTeams().length) {
    notify('还没有可用的专家团，请先在“专家团”中创建并启用团队', 'error');
    return;
  }
  const button = $('sendBtn');
  setBusy(button, true, '处理中…');
  stopTaskStream();
  addMessage('user', message);
  $('messageInput').value = '';
  $('timeline').innerHTML = '';
  state.currentExpertSelection = null;
  $('artifacts').innerHTML = '暂无产物';
  $('artifacts').classList.add('empty');
  try {
    const task = await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        message,
        agent_id: $('agentSelect').value,
        model_id: $('taskModelSelect').value,
        conversation_id: state.conversationId,
        workspace: 'default',
        organization_id: 'local-org',
        user_id: 'local-user',
        executor_type: expertMode ? 'team' : 'agent',
        executor_id: expertMode ? ($('expertTeamSelect').value || null) : $('agentSelect').value,
        attachment_ids: state.uploads.map((x) => x.id),
      }),
    });
    state.uploads = [];
    renderUploads();
    state.currentTask = task;
    renderTaskMeta(task);
    if (task.expert_selection) {
      renderExpertSelectionEvent({
        type: 'expert_selection',
        title: '已选择参与专家',
        content: task.expert_selection.reason || '',
        data: task.expert_selection,
      });
    }
    watchTaskRuntime(task.id);
    startTaskStream(task.id);
  } catch (err) {
    addMessage('agent', `发送失败：${err.message || err}`);
    notify(`发送失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

function renderTaskMeta(task) {
  $('taskMeta').classList.remove('empty');
  const model = state.models.find((m) => m.id === task.model_id);
  const expert = task.executor_type === 'team';
  const executor = expert
    ? state.expertTeams.find((team) => team.id === task.executor_id)?.name || task.executor_id || '自动匹配'
    : state.agents.find((agent) => agent.id === task.agent_id)?.name || task.agent_id;
  $('taskMeta').innerHTML = `<strong>${escapeHtml(task.title || task.id)}</strong>\n状态：${escapeHtml(task.status)}\nID：${escapeHtml(task.id)}\n会话：${escapeHtml(task.conversation_id || state.conversationId)}\n模式：${expert ? '专家协作' : '普通任务'}\n${expert ? '专家团' : '智能体'}：${escapeHtml(executor)}\n模型：${escapeHtml(model?.name || task.model_id || '跟随智能体')}`;
}

const runtimeStatusLabels = {
  pending: '等待中', queued: '已排队', running: '执行中', processing: '处理中',
  completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消',
  cancel_requested: '取消中', paused: '已暂停', interrupted: '已中断',
  waiting: '等待中', waiting_approval: '等待审批', restored: '已恢复', rejected: '已拒绝',
};

const runtimeCommandLabels = {
  cancel: '取消任务', retry: '重试任务', resume: '恢复任务',
  message: '追加指令', restore_checkpoint: '恢复检查点',
};

function runtimeArray(value) {
  return Array.isArray(value) ? value : [];
}

function runtimeStatusLabel(status) {
  return runtimeStatusLabels[status] || status || '未知';
}

function runtimeStatusClass(status) {
  const value = String(status || 'unknown').toLowerCase();
  return /^[a-z0-9_-]+$/.test(value) ? value : 'unknown';
}

function runtimeTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function runtimeActiveRun(runtime = state.taskRuntime) {
  const active = runtime?.active_run;
  if (active && typeof active === 'object') return active;
  if (active != null) return runtimeArray(runtime?.runs).find((run) => String(run.id) === String(active)) || null;
  return null;
}

function currentRuntimeStatus(runtime = state.taskRuntime) {
  return runtimeActiveRun(runtime)?.status || state.currentTask?.status || 'pending';
}

function runtimeIsActive(status) {
  return ['pending', 'queued', 'running', 'processing', 'cancel_requested', 'waiting', 'waiting_approval'].includes(status);
}

function setRuntimeCount(id, count) {
  $(id).textContent = String(count);
}

function runtimeItem(title, status, detail = '', meta = '') {
  return `
    <div class="runtime-item">
      <span class="runtime-dot ${escapeHtml(runtimeStatusClass(status))}"></span>
      <div class="runtime-item-copy"><strong>${escapeHtml(title)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</div>
      <span class="runtime-item-meta">${escapeHtml(meta || runtimeStatusLabel(status))}</span>
    </div>
  `;
}

function resetTaskRuntime() {
  if (state.runtimeTimer) clearTimeout(state.runtimeTimer);
  state.runtimeTimer = null;
  state.runtimeTaskId = null;
  state.taskRuntime = null;
  $('taskRuntime').classList.add('empty');
  $('runtimeSummary').innerHTML = '<div><strong>尚未选择任务</strong><span>创建或打开任务后，可在这里控制运行并恢复现场。</span></div><span id="runtimeLiveStatus" class="runtime-live-status idle">未运行</span>';
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  $('runtimeMessage').value = '';
  [['runtimeRuns', '暂无运行记录'], ['runtimeNodes', '暂无节点记录'], ['runtimeCommands', '暂无排队指令'], ['runtimeCheckpoints', '暂无可恢复检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
}

function renderTaskRuntime(runtime) {
  if (!state.currentTask) return resetTaskRuntime();
  state.taskRuntime = runtime || {};
  const runs = runtimeArray(runtime?.runs);
  const nodes = runtimeArray(runtime?.nodes);
  const checkpoints = runtimeArray(runtime?.checkpoints);
  const commands = runtimeArray(runtime?.commands);
  const active = runtimeActiveRun(runtime);
  const status = active?.status || state.currentTask.status || 'pending';
  const attempt = active?.attempt ?? active?.run_number ?? active?.number;
  const summaryTitle = active ? `${attempt ? `第 ${attempt} 次运行 · ` : ''}${runtimeStatusLabel(status)}` : `任务${runtimeStatusLabel(status)}`;
  const summaryDetail = `${runs.length} 次尝试 · ${nodes.length} 个节点 · ${checkpoints.length} 个检查点`;
  $('taskRuntime').classList.remove('empty');
  $('runtimeSummary').innerHTML = `<div><strong>${escapeHtml(summaryTitle)}</strong><span>${escapeHtml(summaryDetail)}</span></div><span id="runtimeLiveStatus" class="runtime-live-status ${escapeHtml(runtimeStatusClass(status))}">${escapeHtml(runtimeStatusLabel(status))}</span>`;

  $('cancelTaskBtn').disabled = !runtimeIsActive(status);
  $('retryTaskBtn').disabled = runtimeIsActive(status) || (!runs.length && !['failed', 'cancelled', 'interrupted', 'completed', 'succeeded'].includes(status));
  $('resumeTaskBtn').disabled = runtimeIsActive(status) || !checkpoints.length;
  $('runtimeMessage').disabled = !runtimeIsActive(status);
  $('runtimeMessageBtn').disabled = !runtimeIsActive(status);

  setRuntimeCount('runtimeRunCount', runs.length);
  $('runtimeRuns').innerHTML = runs.length ? runs.map((run, index) => {
    const number = run.attempt ?? run.run_number ?? run.number ?? index + 1;
    const times = [run.started_at ? `开始 ${runtimeTime(run.started_at)}` : '', run.finished_at ? `结束 ${runtimeTime(run.finished_at)}` : ''].filter(Boolean).join(' · ');
    return runtimeItem(`第 ${number} 次运行`, run.status, times, runtimeStatusLabel(run.status));
  }).join('') : '<div class="runtime-empty">暂无运行记录</div>';

  setRuntimeCount('runtimeNodeCount', nodes.length);
  $('runtimeNodes').innerHTML = nodes.length ? nodes.map((node, index) => {
    const name = node.title || node.name || node.node_id || node.id || `节点 ${index + 1}`;
    const kind = node.kind || node.type || '执行节点';
    const attemptText = node.attempt || node.run_number ? `第 ${node.attempt || node.run_number} 次 · ` : '';
    const timeText = node.finished_at || node.started_at ? runtimeTime(node.finished_at || node.started_at) : '';
    return runtimeItem(name, node.status, `${attemptText}${kind}`, timeText || runtimeStatusLabel(node.status));
  }).join('') : '<div class="runtime-empty">暂无节点记录</div>';

  setRuntimeCount('runtimeCommandCount', commands.length);
  $('runtimeCommands').innerHTML = commands.length ? commands.map((command) => {
    const type = command.type || command.command || 'command';
    const safeMessage = type === 'message' ? (command.payload?.message || command.payload?.instruction || command.message || '') : '';
    const detail = safeMessage ? `“${String(safeMessage).slice(0, 80)}${String(safeMessage).length > 80 ? '…' : ''}”` : runtimeTime(command.created_at || command.queued_at);
    return runtimeItem(runtimeCommandLabels[type] || type, command.status || 'queued', detail, runtimeStatusLabel(command.status || 'queued'));
  }).join('') : '<div class="runtime-empty">暂无排队指令</div>';

  setRuntimeCount('runtimeCheckpointCount', checkpoints.length);
  $('runtimeCheckpoints').innerHTML = checkpoints.length ? checkpoints.map((checkpoint, index) => {
    const id = checkpoint.id || checkpoint.checkpoint_id;
    const title = checkpoint.label || checkpoint.name || `检查点 ${index + 1}`;
    const location = checkpoint.node_title || checkpoint.node_id || checkpoint.run_id || '任务现场';
    const disabled = checkpoint.restorable === false || !id || runtimeIsActive(status);
    const titleText = runtimeIsActive(status) ? '请先取消当前运行，再恢复检查点' : '从此检查点创建新的运行尝试';
    return `<div class="runtime-item runtime-checkpoint"><span class="runtime-dot checkpoint"></span><div class="runtime-item-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(location)}${checkpoint.created_at ? ` · ${escapeHtml(runtimeTime(checkpoint.created_at))}` : ''}</small></div><button class="runtime-restore secondary" type="button" data-checkpoint-id="${escapeHtml(id || '')}" title="${escapeHtml(titleText)}" ${disabled ? 'disabled' : ''}>恢复</button></div>`;
  }).join('') : '<div class="runtime-empty">暂无可恢复检查点</div>';
  document.querySelectorAll('[data-checkpoint-id]').forEach((button) => {
    button.onclick = () => restoreTaskCheckpoint(button.dataset.checkpointId, button);
  });
}

function renderRuntimeUnavailable(message) {
  if (!state.currentTask) return;
  $('taskRuntime').classList.remove('empty');
  $('runtimeSummary').innerHTML = `<div><strong>运行信息暂不可用</strong><span>${escapeHtml(message || '请稍后刷新任务')}</span></div><span id="runtimeLiveStatus" class="runtime-live-status unknown">未连接</span>`;
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  [['runtimeRuns', '暂无运行记录'], ['runtimeNodes', '暂无节点记录'], ['runtimeCommands', '暂无排队指令'], ['runtimeCheckpoints', '暂无可恢复检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
}

function renderRuntimeLoading() {
  $('taskRuntime').classList.remove('empty');
  $('runtimeSummary').innerHTML = '<div><strong>正在读取运行现场</strong><span>同步运行尝试、节点、指令和检查点…</span></div><span id="runtimeLiveStatus" class="runtime-live-status running">同步中</span>';
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  $('runtimeMessage').value = '';
  [['runtimeRuns', '正在读取运行记录'], ['runtimeNodes', '正在读取节点状态'], ['runtimeCommands', '正在读取指令队列'], ['runtimeCheckpoints', '正在读取检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
}

function scheduleTaskRuntimeRefresh(taskId, delay = 1400) {
  if (!taskId || state.runtimeTaskId !== taskId || state.runtimeTimer) return;
  state.runtimeTimer = setTimeout(() => {
    state.runtimeTimer = null;
    loadTaskRuntime(taskId, { silent: true });
  }, delay);
}

async function loadTaskRuntime(taskId, { silent = false } = {}) {
  if (!taskId || state.currentTask?.id !== taskId) return;
  state.runtimeTaskId = taskId;
  try {
    const runtime = await api(`/api/tasks/${encodeURIComponent(taskId)}/runtime`);
    if (state.currentTask?.id !== taskId) return;
    renderTaskRuntime(runtime);
    if (runtimeIsActive(currentRuntimeStatus(runtime))) scheduleTaskRuntimeRefresh(taskId);
  } catch (err) {
    if (state.currentTask?.id !== taskId) return;
    renderRuntimeUnavailable(err.status === 404 ? '该任务尚未建立可恢复运行记录' : (err.message || '读取失败'));
    if (!silent) notify(`运行信息加载失败：${err.message || err}`, 'error');
    if (runtimeIsActive(state.currentTask?.status)) scheduleTaskRuntimeRefresh(taskId, 2500);
  }
}

function watchTaskRuntime(taskId) {
  if (state.runtimeTimer) clearTimeout(state.runtimeTimer);
  state.runtimeTimer = null;
  state.runtimeTaskId = taskId;
  state.taskRuntime = null;
  renderRuntimeLoading();
  return loadTaskRuntime(taskId, { silent: true });
}

function runtimeCommandError(type, err) {
  if (err.status === 409) return '任务当前状态不允许此操作，请刷新后重试';
  if (err.status === 404) return type === 'restore_checkpoint' ? '检查点不存在或已失效' : '任务不存在或运行记录尚未建立';
  if (err.status === 422 || err.status === 400) return err.message || '提交内容不完整，请检查后重试';
  return err.message || '平台暂时无法处理该操作，请稍后重试';
}

async function sendTaskRuntimeCommand(type, payload = {}, button = null) {
  const taskId = state.currentTask?.id;
  if (!taskId) return notify('请先创建或打开一个任务', 'error');
  if (button) setBusy(button, true, type === 'message' ? '排队中…' : '处理中…');
  try {
    try {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/commands`, {
        method: 'POST', body: JSON.stringify({ type, payload }),
      });
    } catch (err) {
      if (![404, 405].includes(err.status)) throw err;
      let fallbackPath = '';
      if (['cancel', 'retry', 'resume'].includes(type)) fallbackPath = `/api/tasks/${encodeURIComponent(taskId)}/${type}`;
      if (type === 'restore_checkpoint') fallbackPath = `/api/tasks/${encodeURIComponent(taskId)}/checkpoints/${encodeURIComponent(payload.checkpoint_id)}/restore`;
      if (!fallbackPath) throw err;
      await api(fallbackPath, { method: 'POST', body: JSON.stringify(payload) });
    }
    notify({ cancel: '已提交取消请求', retry: '已创建新的运行尝试', resume: '已提交恢复请求', message: '追加指令已加入队列', restore_checkpoint: '已提交检查点恢复请求' }[type] || '操作已提交');
    if (['retry', 'resume', 'restore_checkpoint'].includes(type)) startTaskStream(taskId);
    await refreshCurrentTask(taskId);
    await loadTaskRuntime(taskId, { silent: true });
    return true;
  } catch (err) {
    notify(runtimeCommandError(type, err), 'error');
    return false;
  } finally {
    if (button) setBusy(button, false);
    if (state.taskRuntime && state.currentTask?.id === taskId) renderTaskRuntime(state.taskRuntime);
  }
}

async function submitRuntimeMessage() {
  const input = $('runtimeMessage');
  const message = input.value.trim();
  if (!message) return notify('请先填写要追加的指令', 'error');
  const accepted = await sendTaskRuntimeCommand('message', { message }, $('runtimeMessageBtn'));
  if (accepted) input.value = '';
}

async function restoreTaskCheckpoint(checkpointId, button) {
  if (!checkpointId) return;
  if (!confirm('恢复后将从该检查点创建新的运行尝试，确定继续吗？')) return;
  await sendTaskRuntimeCommand('restore_checkpoint', { checkpoint_id: checkpointId }, button);
}

const taskTerminalStatuses = new Set(['completed', 'failed', 'cancelled']);
const privateTaskEventTypes = new Set(['analysis', 'reasoning', 'thought', 'thinking']);
const streamRetryDelays = [500, 1000, 2000, 4000, 8000];

function stopTaskStream({ clearCursor = true } = {}) {
  state.streamGeneration += 1;
  if (state.eventSource) state.eventSource.close();
  if (state.streamRetryTimer) clearTimeout(state.streamRetryTimer);
  state.eventSource = null;
  state.streamRetryTimer = null;
  state.streamRetryCount = 0;
  state.streamTaskId = null;
  if (clearCursor) {
    state.streamCursor = 0;
    state.seenEventIds = new Set();
  }
}

function taskEventId(event, payload) {
  const value = Number(payload?.id ?? event.lastEventId);
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

function handleTaskStreamEvent(taskId, event) {
  let payload;
  try {
    payload = JSON.parse(event.data);
  } catch (_) {
    return;
  }
  const eventId = taskEventId(event, payload);
  if (eventId && (eventId <= state.streamCursor || state.seenEventIds.has(eventId))) return;
  if (eventId) {
    state.streamCursor = eventId;
    state.seenEventIds.add(eventId);
  }
  if (privateTaskEventTypes.has(String(payload.type || '').toLowerCase())) return;
  scheduleTaskRuntimeRefresh(taskId, 280);
  if (!['answer_delta', 'approval_required'].includes(payload.type)) appendEvent(payload);
  if (payload.type === 'start' && state.currentTask?.id === taskId) {
    state.currentTask.status = 'running';
    renderTaskMeta(state.currentTask);
  }
  if (payload.type === 'answer_reset') resetStreamedAnswer(taskId);
  if (payload.type === 'answer_delta') appendAnswerDelta(taskId, payload.content || '');
  if (['answer', 'error'].includes(payload.type)) finalizeStreamedAnswer(taskId, payload);
  if (payload.type === 'approval_required') renderApproval(taskId, payload);
  if (payload.type === 'install') {
    loadSkillsOnly().catch((err) => notify(`技能列表刷新失败：${err.message || err}`, 'error'));
  }
  if (['done', 'error', 'cancelled'].includes(payload.type)) {
    refreshCurrentTask(taskId);
    loadTasksOnly();
  }
}

function connectTaskStream(taskId, generation) {
  if (generation !== state.streamGeneration || state.streamTaskId !== taskId || state.currentTask?.id !== taskId) return;
  const cursor = state.streamCursor > 0 ? `?cursor=${state.streamCursor}` : '';
  const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events/stream${cursor}`);
  state.eventSource = source;
  source.addEventListener('task_event', (event) => {
    if (generation !== state.streamGeneration || state.eventSource !== source || state.currentTask?.id !== taskId) return;
    state.streamRetryCount = 0;
    handleTaskStreamEvent(taskId, event);
  });
  source.addEventListener('task_status', (event) => {
    if (generation !== state.streamGeneration || state.eventSource !== source) return;
    let payload = {};
    try { payload = JSON.parse(event.data); } catch (_) {}
    if (state.currentTask?.id === taskId && payload.status) {
      state.currentTask.status = payload.status;
      renderTaskMeta(state.currentTask);
    }
    source.close();
    if (state.eventSource === source) state.eventSource = null;
    if (payload.terminal || payload.status === 'waiting_approval') {
      refreshCurrentTask(taskId);
      loadTasksOnly();
    }
  });
  source.onerror = () => {
    if (generation !== state.streamGeneration || state.eventSource !== source) return;
    source.close();
    state.eventSource = null;
    if (state.currentTask?.id !== taskId || taskTerminalStatuses.has(state.currentTask?.status)) return;
    if (state.streamRetryCount >= streamRetryDelays.length) {
      refreshCurrentTask(taskId);
      return;
    }
    const delay = streamRetryDelays[state.streamRetryCount++];
    state.streamRetryTimer = setTimeout(() => {
      state.streamRetryTimer = null;
      connectTaskStream(taskId, generation);
    }, delay);
  };
}

function startTaskStream(taskId, { seenEventIds = null } = {}) {
  const sameTask = state.streamTaskId === taskId;
  const previousCursor = sameTask ? state.streamCursor : 0;
  const previousSeen = sameTask
    ? state.seenEventIds
    : new Set((seenEventIds || []).map(Number).filter((id) => Number.isSafeInteger(id) && id > 0));
  stopTaskStream({ clearCursor: false });
  state.streamTaskId = taskId;
  state.streamCursor = previousCursor;
  state.seenEventIds = previousSeen;
  const generation = state.streamGeneration;
  connectTaskStream(taskId, generation);
}

window.addEventListener('beforeunload', () => stopTaskStream());

function resetStreamedAnswer(taskId) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!streamed) return;
  streamed.dataset.rawContent = '';
  streamed.innerHTML = '<div class="stream-meta"><span></span>已应用追加指令，正在重新生成</div><div class="stream-body"></div>';
}

function appendAnswerDelta(taskId, content) {
  let message = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!message) {
    message = document.createElement('div');
    message.className = 'message agent streaming';
    message.dataset.streamTask = taskId;
    message.dataset.rawContent = '';
    $('conversation').appendChild(message);
  }
  message.dataset.rawContent = (message.dataset.rawContent || '') + content;
  const count = message.dataset.rawContent.length;
  message.innerHTML = `<div class="stream-meta"><span></span>正在实时输出 · ${count} 字</div><div class="stream-body">${renderMarkdown(message.dataset.rawContent)}</div>`;
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function finalizeStreamedAnswer(taskId, payload) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (streamed) {
    renderMessageContent(streamed, payload.content || payload.title);
    streamed.classList.remove('streaming');
    delete streamed.dataset.streamTask;
    streamed.dataset.eventId = String(payload.id);
  } else if (!$('conversation').querySelector(`[data-event-id="${payload.id}"]`)) {
    addMessage('agent', payload.content || payload.title, payload.id);
  }
}

const taskEventLabels = {
  start: '开始执行', intent: '目标理解', plan: '执行计划', plan_progress: '计划进度',
  skill: '技能匹配', model: '模型调用', progress: '执行进度', plan_check: '调用校验',
  tool_call: '调用工具', tool_result: '工具结果', tool_error: '工具失败',
  output_check: '结果验收', answer: '最终答复', done: '执行完成', error: '执行失败',
  approval_required: '等待确认', install: '能力安装', checkpoint: '检查点',
  expert_selection: '专家选择', team_queued: '专家团排队', team_parallel_start: '专家并行',
  team_aggregating: '主管汇总', team_completed: '协作完成', team_partial_failed: '部分失败',
  team_member_retry: '成员重试', policy_decision: '权限决策',
};

function taskEventLabel(type) {
  return taskEventLabels[type] || '执行事件';
}

function expertSelectionMarkup(event) {
  const data = event?.data && typeof event.data === 'object' ? event.data : {};
  const members = Array.isArray(data.members) ? data.members : [];
  const automatic = data.selection_mode === 'automatic';
  const matchedTerms = Array.isArray(data.matched_terms) ? data.matched_terms.filter(Boolean).slice(0, 5) : [];
  const matchText = automatic && matchedTerms.length ? ` · 命中：${matchedTerms.join('、')}` : '';
  const supervisor = data.supervisor?.agent_name || data.supervisor?.agent_id || '';
  const participantMarkup = members.map((member) => {
    const name = member.agent_name || member.name || member.agent_id || '专家';
    const role = member.role || '独立分析';
    return `<div class="expert-participant"><span class="expert-participant-avatar">${escapeHtml(String(name).slice(0, 1))}</span><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(role)}</small></span></div>`;
  }).join('');
  return `
    <div id="expertSelectionSummary" class="expert-selection-card">
      <div class="expert-selection-head">
        <div class="expert-selection-title">
          <span class="expert-selection-mark"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="8" r="2.7"/><circle cx="17" cy="9" r="2.3"/><path d="M3.8 18.5c.5-2.9 1.9-4.5 4.2-4.5s3.7 1.6 4.2 4.5M13.3 18c.4-2.3 1.6-3.7 3.7-3.7s3.3 1.4 3.7 3.7"/></svg></span>
          <span><strong>${escapeHtml(data.team_name || data.team_id || '专家团')}</strong><small>${supervisor ? `主管：${escapeHtml(supervisor)}` : '成员独立分析，主管统一汇总'}${escapeHtml(matchText)}</small></span>
        </div>
        <span class="expert-selection-mode">${automatic ? '自动匹配' : '手动指定'}</span>
      </div>
      <div class="expert-selection-reason">${escapeHtml(data.reason || event?.content || '已根据当前任务选择参与专家。')}</div>
      ${participantMarkup ? `<div class="expert-participants">${participantMarkup}</div>` : ''}
    </div>`;
}

function renderExpertSelectionEvent(event) {
  state.currentExpertSelection = event;
  const current = $('expertSelectionSummary');
  if (current) {
    current.outerHTML = expertSelectionMarkup(event);
  } else if ($('executionPlan')) {
    $('executionPlan').insertAdjacentHTML('afterbegin', expertSelectionMarkup(event));
  } else {
    $('timeline').insertAdjacentHTML('beforeend', expertSelectionMarkup(event));
  }
  if (state.workbenchMode === 'expert' && $('expertTeamSelect')) {
    const automatic = event.data?.selection_mode === 'automatic';
    const teamId = event.data?.team_id || '';
    $('expertTeamSelect').value = automatic ? '' : (enabledWorkbenchTeams().some((team) => team.id === teamId) ? teamId : '');
    renderWorkbenchMode();
  }
}

function appendEvent(event) {
  if (['analysis', 'reasoning', 'thought', 'thinking'].includes(event.type)) return;
  if (event.type === 'checkpoint') return;
  if (event.type === 'policy_decision' && event.data?.outcome === 'allow') return;
  if (event.type === 'expert_selection') {
    renderExpertSelectionEvent(event);
    return;
  }
  if (event.type === 'plan') {
    renderExecutionPlan(event);
    return;
  }
  if (event.type === 'plan_progress') {
    updateExecutionProgress(event);
    return;
  }
  if ($('executionPlan') && ['intent', 'skill', 'model', 'plan_check', 'tool_call', 'tool_result', 'tool_error', 'output_check', 'progress'].includes(event.type)) {
    appendPlanDetail(event);
    return;
  }
  if ($('executionPlan') && ['answer', 'done'].includes(event.type)) {
    return;
  }
  const div = document.createElement('div');
  div.className = 'event';
  div.innerHTML = `
    <div class="event-title">
      <span>${escapeHtml(event.title)}</span>
      <span class="badge ${escapeHtml(event.type)}">${escapeHtml(taskEventLabel(event.type))}</span>
    </div>
    ${event.content ? `<div class="event-content">${escapeHtml(event.content)}</div>` : ''}
  `;
  $('timeline').appendChild(div);
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

const planStatusLabels = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
};

function renderExecutionPlan(event) {
  const plan = event.data?.plan || {};
  const nodes = Array.isArray(plan.nodes) ? plan.nodes : [];
  const confirmation = plan.goal_confirmation || {};
  const criteria = Array.isArray(plan.acceptance_criteria) ? plan.acceptance_criteria : [];
  $('timeline').innerHTML = `
    <div id="executionPlan" class="execution-tree" data-tool-node-id="${escapeHtml(plan.tool_node_id || 'execute')}">
      ${state.currentExpertSelection ? expertSelectionMarkup(state.currentExpertSelection) : ''}
      <div class="execution-goal">
        <div><span>目标</span>${escapeHtml(plan.goal || event.content || '')}</div>
        ${confirmation.label ? `<div class="goal-confirmation ${escapeHtml(confirmation.status || '')}"><b>✓</b><strong>${escapeHtml(confirmation.label)}</strong><small>${escapeHtml(confirmation.message || '')}</small></div>` : ''}
      </div>
      <div class="execution-nodes">
        ${nodes.map((node, index) => renderPlanNode(node, index)).join('')}
      </div>
      ${criteria.length ? `<div class="acceptance-panel"><div class="acceptance-title"><span>最终验收标准</span><small id="acceptanceSummary">等待执行完成后逐项检查</small></div><div id="acceptanceCriteria">${criteria.map(renderAcceptanceCriterion).join('')}</div></div>` : ''}
    </div>
  `;
  $('executionPlan').querySelectorAll('.plan-node-head').forEach((head) => {
    head.onclick = () => {
      const node = head.closest('.plan-node');
      if (node.classList.contains('has-children')) node.classList.toggle('expanded');
    };
  });
}

function renderPlanNode(node, index) {
  const children = Array.isArray(node.children) ? node.children : [];
  const status = node.status || 'pending';
  return `
    <div class="plan-node ${escapeHtml(status)} ${children.length ? 'has-children' : ''}" data-node-id="${escapeHtml(node.id)}">
      <button class="plan-node-head" type="button">
        <span class="plan-node-index">${index + 1}</span>
        <span class="plan-node-copy"><strong>${escapeHtml(node.title)}</strong><small class="plan-node-message">${planStatusLabels[status]}</small></span>
        <span class="plan-status">${planStatusLabels[status]}</span>
        ${children.length ? '<span class="plan-chevron">⌄</span>' : ''}
      </button>
      ${children.length ? `<div class="plan-node-children">${children.map(renderPlanChild).join('')}</div>` : ''}
      <div class="plan-node-details"></div>
    </div>
  `;
}

function renderAcceptanceCriterion(item) {
  const status = item.status || 'pending';
  const symbol = status === 'passed' ? '✓' : status === 'failed' ? '!' : '·';
  return `<div class="acceptance-item ${escapeHtml(status)}" data-criterion-id="${escapeHtml(item.id || '')}"><span>${symbol}</span><div><strong>${escapeHtml(item.title || '')}</strong>${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ''}</div></div>`;
}

function renderPlanChild(child) {
  const status = child.status || 'pending';
  const kind = { agent: '专家', skill: '技能', mcp: '工具', tool: '工具', model: '模型', detail: '明细' }[child.kind] || '明细';
  return `
    <div class="plan-child ${escapeHtml(status)}" data-child-id="${escapeHtml(child.id)}">
      <span class="plan-dot"></span>
      <span class="plan-child-copy"><strong>${escapeHtml(child.title)}</strong><small>${escapeHtml(kind)}</small></span>
      <span class="plan-child-status">${planStatusLabels[status]}</span>
    </div>
  `;
}

function updateExecutionProgress(event) {
  const data = event.data || {};
  const tree = $('executionPlan');
  if (!tree) return;
  const node = [...tree.querySelectorAll('.plan-node')].find((item) => item.dataset.nodeId === data.node_id);
  if (!node) return;
  const status = data.status || 'running';
  if (data.child_id) {
    let children = node.querySelector('.plan-node-children');
    if (!children) {
      children = document.createElement('div');
      children.className = 'plan-node-children';
      node.appendChild(children);
      node.classList.add('has-children');
      const head = node.querySelector('.plan-node-head');
      if (!head.querySelector('.plan-chevron')) head.insertAdjacentHTML('beforeend', '<span class="plan-chevron">⌄</span>');
    }
    let child = [...children.querySelectorAll('.plan-child')].find((item) => item.dataset.childId === data.child_id);
    if (!child) {
      children.insertAdjacentHTML('beforeend', renderPlanChild({ id: data.child_id, title: data.child_title || data.child_id, kind: data.child_kind, status }));
      child = [...children.querySelectorAll('.plan-child')].find((item) => item.dataset.childId === data.child_id);
    }
    setPlanElementStatus(child, status, '.plan-child-status');
    node.classList.add('expanded');
    if (status === 'failed') setPlanElementStatus(node, 'failed', '.plan-status');
    else if (!node.classList.contains('completed')) setPlanElementStatus(node, 'running', '.plan-status');
  } else {
    setPlanElementStatus(node, status, '.plan-status');
    if (status === 'running') node.classList.add('expanded');
  }
  const message = node.querySelector('.plan-node-message');
  if (message) message.textContent = event.content || planStatusLabels[status];
  tree.querySelectorAll('.plan-node').forEach((item) => {
    if (item !== node && item.classList.contains('running')) item.classList.remove('expanded');
  });
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

function appendPlanDetail(event) {
  const tree = $('executionPlan');
  if (!tree) return;
  if (event.type === 'output_check' && Array.isArray(event.data?.criteria)) {
    const criteria = $('acceptanceCriteria');
    if (criteria) criteria.innerHTML = event.data.criteria.map(renderAcceptanceCriterion).join('');
    const summary = $('acceptanceSummary');
    if (summary) {
      summary.textContent = event.data.passed ? `全部 ${event.data.criteria.length} 项通过` : `${event.data.criteria.filter((item) => item.status === 'failed').length} 项未通过`;
      summary.className = event.data.passed ? 'passed' : 'failed';
    }
  }
  const mapping = {
    intent: 'understand', skill: 'understand', model: 'execute', progress: 'execute',
    plan_check: tree.dataset.toolNodeId || 'execute', tool_call: tree.dataset.toolNodeId || 'execute',
    tool_result: tree.dataset.toolNodeId || 'execute', tool_error: tree.dataset.toolNodeId || 'execute', output_check: 'validate',
  };
  const nodeId = mapping[event.type] || 'execute';
  const node = [...tree.querySelectorAll('.plan-node')].find((item) => item.dataset.nodeId === nodeId);
  if (!node) return;
  const details = node.querySelector('.plan-node-details');
  if (!details) return;
  const detail = document.createElement('div');
  detail.className = `plan-detail ${escapeHtml(event.type)}`;
  const label = { plan_check: '调用前校验', tool_call: '调用参数', tool_result: '工具结果', tool_error: '工具失败', output_check: '最终验收', model: '模型', skill: '技能', intent: '目标解析', progress: '进度' }[event.type] || taskEventLabel(event.type);
  let content = event.content || '';
  let meta = '';
  if (event.type === 'tool_call') {
    const args = event.data?.arguments || {};
    content = Object.entries(args).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ') || '无额外参数';
    meta = `${event.data?.server_id || ''}.${event.data?.tool_name || ''}`;
  } else if (event.type === 'tool_result') {
    meta = event.data?.duration_ms ? `耗时 ${event.data.duration_ms} ms` : '';
    if (event.data?.artifact?.name) content = `${content} · ${event.data.artifact.name}`;
  }
  detail.innerHTML = `<span class="plan-detail-kind">${escapeHtml(label)}</span><div><strong>${escapeHtml(event.title || label)}</strong>${content ? `<small>${escapeHtml(content)}</small>` : ''}</div>${meta ? `<em>${escapeHtml(meta)}</em>` : ''}`;
  details.appendChild(detail);
  node.classList.add('has-children', 'expanded');
  const head = node.querySelector('.plan-node-head');
  if (!head.querySelector('.plan-chevron')) head.insertAdjacentHTML('beforeend', '<span class="plan-chevron">⌄</span>');
}

function setPlanElementStatus(element, status, labelSelector) {
  element.classList.remove('pending', 'running', 'completed', 'failed');
  element.classList.add(status);
  const label = element.querySelector(labelSelector);
  if (label) label.textContent = planStatusLabels[status] || status;
}

function renderApproval(taskId, event = {}) {
  const isInstall = event.data?.action === 'install_recommended_skill';
  const recommendations = event.data?.recommendations || [];
  const div = document.createElement('div');
  div.className = `event ${isInstall ? 'skill-recommendation' : ''}`;
  div.innerHTML = `
    <div class="event-title"><span>${isInstall ? '发现可补充的 Skill' : '人工审批'}</span><span class="badge approval_required">${isInstall ? '能力补充' : '等待确认'}</span></div>
    <div class="event-content">${escapeHtml(event.content || (isInstall ? '当前能力不足，是否安装推荐 Skill？' : '是否批准执行敏感操作？'))}</div>
    ${recommendations.map((item) => `<div class="recommendation-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.description || '')}</span><small>${escapeHtml(item.source_label || '内置目录')}</small></div>`).join('')}
    <div class="approval-actions">
      <button id="approveYes">${isInstall ? '确认安装' : '批准'}</button>
      <button id="approveNo" class="secondary">${isInstall ? '暂不安装' : '拒绝'}</button>
    </div>
  `;
  $('timeline').appendChild(div);
  $('approveYes').onclick = () => approveTask(taskId, true);
  $('approveNo').onclick = () => approveTask(taskId, false);
}

async function approveTask(taskId, approved) {
  await api(`/api/tasks/${taskId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ approved, note: approved ? '前端批准' : '前端拒绝' }),
  });
  startTaskStream(taskId);
}

async function refreshCurrentTask(taskId) {
  try {
    const task = await api(`/api/tasks/${taskId}`);
    state.currentTask = task;
    renderTaskMeta(task);
    renderArtifacts(task.artifacts || []);
    const answer = [...(task.events || [])].reverse().find((event) => ['answer', 'error'].includes(event.type));
    if (answer && !$('conversation').querySelector(`[data-event-id="${answer.id}"]`)) addMessage('agent', answer.content || answer.title, answer.id);
    await loadTaskRuntime(taskId, { silent: true });
  } catch (err) {
    console.error(err);
  }
}

function renderArtifacts(artifacts) {
  const box = $('artifacts');
  if (!artifacts.length) {
    box.innerHTML = '暂无产物';
    box.classList.add('empty');
    return;
  }
  box.classList.remove('empty');
  box.innerHTML = artifacts.map((a) => {
    const downloadUrl = safeArtifactDownloadUrl(a.download_url);
    return `
      <div class="artifact">
        <span>${escapeHtml(a.name)} <span class="small">${escapeHtml(a.kind)}</span></span>
        <span class="artifact-actions"><button type="button" class="text-button" data-preview-artifact="${escapeHtml(a.id || '')}" ${a.id ? '' : 'disabled'}>预览</button>${downloadUrl ? `<a href="${escapeHtml(downloadUrl)}" target="_blank" rel="noopener noreferrer">下载</a>` : ''}</span>
      </div>
    `;
  }).join('');
  box.querySelectorAll('[data-preview-artifact]').forEach((button) => {
    button.onclick = () => openArtifactPreview(button.dataset.previewArtifact).catch((err) => notify(`产物预览失败：${err.message || err}`, 'error'));
  });
}

function renderSkills() {
  $('skillList').innerHTML = state.skills.map((s) => `
    <div class="card ${state.selectedSkill?.id === s.id ? 'active' : ''}" data-skill="${escapeHtml(s.id)}">
      <div class="card-title"><span>${escapeHtml(s.name)}</span><span class="status">${s.enabled ? 'enabled' : 'disabled'}</span></div>
      <div class="card-desc">${escapeHtml(s.description)}</div>
      <div class="small">${escapeHtml(s.id)} · ${escapeHtml(s.category)} · ${escapeHtml(s.version)} · ${s.file_count || 0} files</div>
    </div>
  `).join('');
  document.querySelectorAll('[data-skill]').forEach((el) => el.onclick = () => selectSkill(el.dataset.skill));
}

async function selectSkill(id) {
  const skill = await api(`/api/skills/${id}`);
  state.selectedSkill = skill;
  $('skillEditorTitle').textContent = skill.name;
  $('skillId').value = skill.id;
  $('skillId').disabled = true;
  $('skillName').value = skill.name;
  $('skillCategory').value = skill.category;
  $('skillVersion').value = skill.version;
  $('skillDescription').value = skill.description;
  $('skillMcps').value = (skill.required_mcps || []).join(',');
  $('skillEnabled').checked = !!skill.enabled;
  $('skillContent').value = skill.content;
  $('skillPackageStatus').textContent = (skill.package_missing || []).length
    ? `缺少 ${skill.package_missing.length} 个被 SKILL.md 引用的文件：${skill.package_missing.join('、')}`
    : `包结构完整 · ${skill.file_count || 0} 个文件`;
  $('skillPackageStatus').className = `small ${(skill.package_missing || []).length ? 'package-warning' : 'package-ok'}`;
  $('deleteSkillBtn').classList.toggle('hidden', skill.category === 'builtin');
  $('exportSkillBtn').disabled = false;
  await loadSkillFiles(id);
  renderSkills();
}

function newSkill() {
  state.selectedSkill = null;
  $('skillEditorTitle').textContent = '新建技能';
  $('skillId').disabled = false;
  $('skillId').value = 'custom_skill_' + Math.floor(Math.random() * 1000);
  $('skillName').value = '自定义技能';
  $('skillCategory').value = 'custom';
  $('skillVersion').value = '0.1.0';
  $('skillDescription').value = '描述这个技能适合处理哪些任务。';
  $('skillMcps').value = 'report';
  $('skillEnabled').checked = true;
  $('skillContent').value = `---\nid: ${$('skillId').value}\nname: 自定义技能\ndescription: 描述这个技能适合处理哪些任务。\ncategory: custom\nversion: 0.1.0\nrequired_mcps: report\n---\n\n# 自定义技能\n\n## 使用条件\n\n说明什么场景触发。\n\n## 执行流程\n\n1. 第一步。\n2. 第二步。\n\n## 输出格式\n\n说明最终结果怎么输出。\n`;
  $('deleteSkillBtn').classList.add('hidden');
  $('exportSkillBtn').disabled = true;
  $('skillPackageStatus').textContent = '保存 Skill 后可维护包文件';
  $('skillPackageStatus').className = 'small';
  state.skillFiles = [];
  state.selectedSkillFile = null;
  renderSkillFiles();
}

async function loadSkillFiles(skillId) {
  state.skillFiles = await api(`/api/skills/${encodeURIComponent(skillId)}/files`);
  state.selectedSkillFile = null;
  renderSkillFiles();
  const first = state.skillFiles.find((item) => item.path === 'SKILL.md') || state.skillFiles[0];
  if (first) await selectSkillFile(first.path);
}

function renderSkillFiles() {
  $('skillFileList').innerHTML = state.skillFiles.length ? state.skillFiles.map((file) => `
    <button type="button" class="skill-file ${state.selectedSkillFile?.path === file.path ? 'active' : ''}" data-skill-file="${escapeHtml(file.path)}">
      <span>${escapeHtml(file.path)}</span><small>${file.is_binary ? 'binary' : `${Math.ceil(file.size / 1024) || 1} KB`}</small>
    </button>
  `).join('') : '<div class="meta empty">当前 Skill 只有主配置，尚无附属文件</div>';
  document.querySelectorAll('[data-skill-file]').forEach((el) => el.onclick = () => selectSkillFile(el.dataset.skillFile));
  if (!state.selectedSkillFile) {
    $('skillFilePath').value = '';
    $('skillFileContent').value = '';
    $('skillFileContent').disabled = true;
    $('saveSkillFileBtn').disabled = true;
    $('deleteSkillFileBtn').classList.add('hidden');
    $('skillFileMeta').textContent = '尚未选择文件';
  }
}

async function selectSkillFile(path) {
  if (!state.selectedSkill) return;
  const file = await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${path.split('/').map(encodeURIComponent).join('/')}`);
  state.selectedSkillFile = file;
  $('skillFilePath').value = file.path;
  $('skillFilePath').disabled = true;
  $('skillFileContent').value = file.content || '';
  $('skillFileContent').disabled = !!file.is_binary;
  $('saveSkillFileBtn').disabled = !!file.is_binary;
  $('deleteSkillFileBtn').classList.toggle('hidden', file.path === 'SKILL.md');
  $('skillFileMeta').textContent = `${file.content_type} · ${file.size} bytes${file.is_binary ? ' · 二进制文件仅支持查看元数据' : ''}`;
  renderSkillFiles();
}

function newSkillFile() {
  if (!state.selectedSkill) return notify('请先保存或选择一个 Skill', 'error');
  state.selectedSkillFile = { path: '', content: '', is_binary: false, is_new: true };
  $('skillFilePath').disabled = false;
  $('skillFilePath').value = 'references/rules.md';
  $('skillFileContent').disabled = false;
  $('skillFileContent').value = '# Rules\n\n在这里维护规则、脚本说明或参考资料。\n';
  $('saveSkillFileBtn').disabled = false;
  $('deleteSkillFileBtn').classList.add('hidden');
  $('skillFileMeta').textContent = '新文件';
  renderSkillFiles();
}

async function saveSkillFile() {
  if (!state.selectedSkill) return;
  const path = $('skillFilePath').value.trim();
  if (!path) return notify('请填写文件路径', 'error');
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${encodedPath}`, { method: 'PUT', body: JSON.stringify({ content: $('skillFileContent').value }) });
  if (path === 'SKILL.md') {
    state.selectedSkill = await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}`);
    $('skillContent').value = state.selectedSkill.content;
  }
  await loadSkillFiles(state.selectedSkill.id);
  await selectSkillFile(path);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${path}”已保存`);
}

async function deleteSkillFile() {
  const file = state.selectedSkillFile;
  if (!state.selectedSkill || !file || file.path === 'SKILL.md') return;
  if (!confirm(`确定删除文件“${file.path}”吗？`)) return;
  const encodedPath = file.path.split('/').map(encodeURIComponent).join('/');
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${encodedPath}`, { method: 'DELETE' });
  await loadSkillFiles(state.selectedSkill.id);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${file.path}”已删除`);
}

async function uploadSkillFile(file) {
  if (!state.selectedSkill || !file) return notify('请先选择一个 Skill', 'error');
  let path = $('skillFilePath').value.trim();
  if (!path || path === 'SKILL.md') path = file.name;
  const form = new FormData();
  form.append('file', file);
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/upload?path=${encodeURIComponent(path)}`, { method: 'POST', body: form });
  await loadSkillFiles(state.selectedSkill.id);
  await selectSkillFile(path);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${path}”已上传`);
}

function exportSelectedSkill() {
  if (!state.selectedSkill) return notify('请先选择一个 Skill', 'error');
  window.location.href = `/api/skills/${encodeURIComponent(state.selectedSkill.id)}/export`;
}

async function deleteSkill() {
  const skill = state.selectedSkill;
  if (!skill || skill.category === 'builtin') return;
  if (!confirm(`确定卸载技能“${skill.name}”吗？`)) return;
  await api(`/api/skills/${skill.id}`, { method: 'DELETE' });
  state.skills = await api('/api/skills');
  newSkill(); renderSkills(); notify(`技能“${skill.name}”已卸载`);
}

async function saveSkill() {
  const button = $('saveSkillBtn');
  setBusy(button, true);
  try {
  const payload = {
    id: $('skillId').value.trim(),
    name: $('skillName').value.trim(),
    description: $('skillDescription').value.trim(),
    category: $('skillCategory').value.trim() || 'custom',
    version: $('skillVersion').value.trim() || '0.1.0',
    content: $('skillContent').value,
    enabled: $('skillEnabled').checked,
    required_mcps: $('skillMcps').value.split(',').map((x) => x.trim()).filter(Boolean),
  };
  if (state.selectedSkill) {
    await api(`/api/skills/${state.selectedSkill.id}`, { method: 'PUT', body: JSON.stringify(payload) });
  } else {
    await api('/api/skills', { method: 'POST', body: JSON.stringify(payload) });
  }
  state.skills = await api('/api/skills');
  renderSkills();
  await selectSkill(payload.id);
  notify(`技能“${payload.name}”已保存`);
  } catch (err) {
    notify(`技能保存失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

function renderMcp() {
  $('mcpList').innerHTML = state.mcp.map((m) => `
    <div class="card ${state.selectedMcp?.id === m.id ? 'active' : ''}" data-mcp="${escapeHtml(m.id)}">
      <div class="card-title"><span>${escapeHtml(m.name)}</span><span class="status">${escapeHtml(m.kind)}</span></div>
      <div class="card-desc">${escapeHtml(m.description)}</div>
      <div class="small">${escapeHtml(m.id)} · ${m.tools?.length || 0} tools</div>
    </div>
  `).join('');
  document.querySelectorAll('[data-mcp]').forEach((el) => el.onclick = () => selectMcp(el.dataset.mcp));
}

async function selectMcp(id) {
  const mcp = await api(`/api/mcp/${id}`);
  state.selectedMcp = mcp;
  $('mcpTitle').textContent = mcp.name;
  $('mcpDetails').textContent = `ID：${mcp.id}\n类型：${mcp.kind}\n状态：${mcp.enabled ? '启用' : '停用'}\n描述：${mcp.description}`;
  $('mcpId').value = mcp.id; $('mcpId').disabled = true; $('mcpName').value = mcp.name; $('mcpKind').value = mcp.kind;
  $('mcpDescription').value = mcp.description; $('mcpConfig').value = formatJson(mcp.config || {}); $('mcpEnabled').checked = !!mcp.enabled;
  $('deleteMcpBtn').classList.toggle('hidden', mcp.kind === 'builtin');
  $('toolList').innerHTML = (mcp.tools || []).map((t) => `
    <div class="tool-item" data-tool="${escapeHtml(t.name)}">
      <strong>${escapeHtml(t.name)}</strong>
      <span>${escapeHtml(t.description)}</span>
      <details><summary>schema</summary><pre>${escapeHtml(formatJson(t.input_schema || {}))}</pre></details>
    </div>
  `).join('');
  document.querySelectorAll('[data-tool]').forEach((el) => el.onclick = () => {
    $('testServerId').value = mcp.id;
    $('testToolName').value = el.dataset.tool;
    const tool = (mcp.tools || []).find((t) => t.name === el.dataset.tool);
    $('toolArgs').value = exampleArgs(mcp.id, tool?.name);
  });
  renderMcp();
}

function newMcp() {
  state.selectedMcp = null; $('mcpTitle').textContent = '添加工具服务'; $('mcpDetails').textContent = '连接本地、远程或 HTTP 工具服务。';
  $('mcpId').disabled = false; $('mcpId').value = 'my-tools'; $('mcpName').value = '我的工具服务'; $('mcpKind').value = 'mcp_stdio'; $('mcpDescription').value = '';
  $('mcpConfig').value = formatJson({ command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '/workspace'], env: {} }); $('mcpEnabled').checked = true; $('toolList').innerHTML = '';
  $('deleteMcpBtn').classList.add('hidden');
}

async function deleteMcp() {
  const server = state.selectedMcp;
  if (!server || server.kind === 'builtin') return;
  if (!confirm(`确定卸载工具服务“${server.name}”吗？`)) return;
  await api(`/api/mcp/${server.id}`, { method: 'DELETE' });
  state.mcp = await api('/api/mcp');
  newMcp(); renderMcp(); notify(`工具服务“${server.name}”已卸载`);
}

async function saveMcp() {
  const button = $('saveMcpBtn');
  setBusy(button, true);
  try {
  const payload = { id: $('mcpId').value.trim(), name: $('mcpName').value.trim(), kind: $('mcpKind').value, description: $('mcpDescription').value.trim(), enabled: $('mcpEnabled').checked, config: JSON.parse($('mcpConfig').value || '{}'), tools: state.selectedMcp?.tools || [] };
  if (!payload.id || !payload.name) throw new Error('请填写 ID 和名称');
  if (state.selectedMcp) await api(`/api/mcp/${state.selectedMcp.id}`, { method: 'PUT', body: JSON.stringify(payload) });
  else await api('/api/mcp', { method: 'POST', body: JSON.stringify(payload) });
  state.mcp = await api('/api/mcp'); await selectMcp(payload.id);
  notify(`工具服务“${payload.name}”已保存`);
  return payload;
  } catch (err) {
    notify(`工具服务保存失败：${err.message || err}`, 'error');
    throw err;
  } finally {
    setBusy(button, false);
  }
}

async function discoverMcp() {
  try { await saveMcp(); const tools = await api(`/api/mcp/${$('mcpId').value.trim()}/discover`, { method: 'POST' }); $('toolResult').textContent = formatJson(tools); state.mcp = await api('/api/mcp'); await selectMcp($('mcpId').value.trim()); }
  catch (err) { $('toolResult').textContent = String(err.message || err); }
}

function exampleArgs(serverId, toolName) {
  const server = state.mcp.find((item) => item.id === serverId);
  const tool = (server?.tools || []).find((item) => item.name === toolName);
  const schema = tool?.input_schema || {};
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  const valueFor = (definition = {}) => {
    if (definition.default !== undefined) return definition.default;
    if (Array.isArray(definition.enum) && definition.enum.length) return definition.enum[0];
    if (definition.type === 'array') return [];
    if (definition.type === 'object') return {};
    if (definition.type === 'integer' || definition.type === 'number') return 0;
    if (definition.type === 'boolean') return false;
    return '';
  };
  return formatJson(Object.fromEntries(
    Object.entries(properties)
      .filter(([name, definition]) => required.has(name) || definition.default !== undefined)
      .map(([name, definition]) => [name, valueFor(definition)]),
  ));
}

async function invokeTool() {
  try {
    const serverId = $('testServerId').value.trim();
    const toolName = $('testToolName').value.trim();
    const args = JSON.parse($('toolArgs').value || '{}');
    const result = await api(`/api/mcp/${serverId}/tools/${toolName}/invoke`, {
      method: 'POST',
      body: JSON.stringify({ arguments: args }),
    });
    $('toolResult').textContent = formatJson(result);
  } catch (err) {
    $('toolResult').textContent = String(err.message || err);
  }
}

function renderAgents() {
  $('agentList').innerHTML = state.agents.map((a) => `
    <div class="agent-card ${state.selectedAgent?.id === a.id ? 'active' : ''}" data-agent="${escapeHtml(a.id)}">
      <div class="agent-card-head">
        <div class="agent-avatar ${agentAvatarTone(a)}">${agentIconSvg(a)}</div>
        <div class="agent-card-copy"><div class="card-title">${escapeHtml(a.name)}</div><div class="card-desc">${escapeHtml(a.description)}</div></div>
        <span class="status">${escapeHtml(a.model)}</span>
      </div>
      <div class="kv">
        <strong>ID</strong><span>${escapeHtml(a.id)}</span>
        <strong>技能</strong><span>${escapeHtml((a.skills || []).join(', '))}</span>
        <strong>工具</strong><span>${escapeHtml((a.mcp_servers || []).join(', '))}</span>
        <strong>权限</strong><span>${escapeHtml(formatJson(a.permissions || {}))}</span>
      </div>
    </div>
  `).join('');
  document.querySelectorAll('[data-agent]').forEach((el) => el.onclick = () => selectAgent(el.dataset.agent));
}

function newAgent() { state.selectedAgent = null; $('agentEditorTitle').textContent = '新建智能体'; $('agentId').disabled = false; $('agentId').value = 'custom-agent'; $('agentName').value = '我的智能体'; $('agentDescription').value = ''; $('agentPrompt').value = '请理解用户目标，优先使用已配置的技能和工具完成任务。'; $('agentSkills').value = 'general_task,report_generation'; $('agentMcps').value = 'report'; $('agentPermissions').value = '{}'; $('agentModel').value = 'deterministic'; renderAgents(); }
function selectAgent(id) { const a = state.agents.find((x) => x.id === id); if (!a) return; state.selectedAgent = a; $('agentEditorTitle').textContent = a.name; $('agentId').disabled = true; $('agentId').value = a.id; $('agentName').value = a.name; $('agentDescription').value = a.description; $('agentPrompt').value = a.system_prompt; $('agentSkills').value = (a.skills || []).join(','); $('agentMcps').value = (a.mcp_servers || []).join(','); $('agentPermissions').value = formatJson(a.permissions || {}); $('agentModel').value = a.model; renderAgents(); }
async function saveAgent() { const list = (id) => $(id).value.split(',').map((x) => x.trim()).filter(Boolean); const payload = { id: $('agentId').value.trim(), name: $('agentName').value.trim(), description: $('agentDescription').value.trim(), model: $('agentModel').value, system_prompt: $('agentPrompt').value, skills: list('agentSkills'), mcp_servers: list('agentMcps'), permissions: JSON.parse($('agentPermissions').value || '{}') }; if (state.selectedAgent) await api(`/api/agents/${state.selectedAgent.id}`, { method: 'PUT', body: JSON.stringify(payload) }); else await api('/api/agents', { method: 'POST', body: JSON.stringify(payload) }); state.agents = await api('/api/agents'); renderAgentsSelect(); selectAgent(payload.id); }

const expertVisibilityLabels = { private: '本机使用者', workspace: '当前工作区', organization: '当前实例', public: '实例内公共' };
const expertRunStatusLabels = {
  queued: '已排队', running: '成员执行中', aggregating: '主管汇总中', partial_failed: '部分失败',
  waiting_approval: '等待审批', completed: '已完成', failed: '失败', cancelled: '已取消',
};

function expertScopeValues() {
  return { organization_id: 'local-org', workspace_id: 'default', user_id: 'local-user' };
}

function expertQuery({ includeDisabled = false } = {}) {
  const query = new URLSearchParams(expertScopeValues());
  if (includeDisabled) query.set('include_disabled', 'true');
  return query.toString();
}

function parseExpertJson(id, label, expected = 'object') {
  const raw = $(id).value.trim();
  let value;
  try { value = JSON.parse(raw || (expected === 'array' ? '[]' : '{}')); }
  catch (_) { throw new Error(`${label}不是有效的 JSON`); }
  if (expected === 'array' && !Array.isArray(value)) throw new Error(`${label}必须是 JSON 数组`);
  if (expected === 'object' && (!value || Array.isArray(value) || typeof value !== 'object')) throw new Error(`${label}必须是 JSON 对象`);
  return value;
}

function validateExpertId(value, label) {
  if (!value) throw new Error(`请填写${label}`);
  if (!/^[A-Za-z0-9_-]{2,80}$/.test(value)) throw new Error(`${label}只能包含字母、数字、短横线和下划线，长度 2–80`);
}

function expertOwns(item, ownerKey = 'owner_user_id') {
  const scope = expertScopeValues();
  return item
    && (item.organization_id || 'local-org') === scope.organization_id
    && (item.workspace_id || 'default') === scope.workspace_id
    && (item[ownerKey] || 'local-user') === scope.user_id;
}

async function loadExpertWorkspace({ preserveSelection = false } = {}) {
  const templateId = preserveSelection ? state.selectedExpertTemplate?.id : '';
  const teamId = preserveSelection ? state.selectedExpertTeam?.id : '';
  const [templates, installations, teams] = await Promise.all([
    api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`),
    api(`/api/expert-installations?${expertQuery()}`),
    api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`),
  ]);
  state.expertTemplates = Array.isArray(templates) ? templates : [];
  state.expertInstallations = Array.isArray(installations) ? installations : [];
  state.expertTeams = Array.isArray(teams) ? teams : [];
  state.selectedExpertTemplate = templateId ? state.expertTemplates.find((item) => item.id === templateId) || null : null;
  state.selectedExpertTeam = teamId ? state.expertTeams.find((item) => item.id === teamId) || null : null;
  renderExpertTemplates();
  renderExpertInstallations();
  renderExpertTeams();
  renderExpertRunModelOptions();
  if (state.selectedExpertTemplate) selectExpertTemplate(state.selectedExpertTemplate.id, { reload: false });
  else newExpertTemplate({ preserveLists: true });
  if (state.selectedExpertTeam) await selectExpertTeam(state.selectedExpertTeam.id, { reload: false, loadRuns: true });
  else newExpertTeam({ preserveLists: true });
  renderWorkbenchTeamOptions();
  renderWorkbenchMode();
}

function renderExpertTemplates() {
  $('expertTemplateCount').textContent = `${state.expertTemplates.length} 个模板`;
  $('expertTemplateList').innerHTML = state.expertTemplates.map((item) => {
    const manifest = item.manifest && typeof item.manifest === 'object' ? item.manifest : {};
    const capabilityCount = (Array.isArray(manifest.skills) ? manifest.skills.length : 0) + (Array.isArray(manifest.mcp_servers) ? manifest.mcp_servers.length : 0);
    return `
      <button class="card expert-template-card ${state.selectedExpertTemplate?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-expert-template="${escapeHtml(item.id)}" type="button">
        <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? `v${escapeHtml(item.version)}` : '已停用'}</span></div>
        <div class="card-desc">${escapeHtml(item.description || '未填写模板说明')}</div>
        <div class="expert-card-meta"><span>${escapeHtml(expertVisibilityLabels[item.visibility] || item.visibility)}</span><span>${capabilityCount} 项能力</span><span>${escapeHtml(item.id)}</span></div>
      </button>`;
  }).join('') || '<div class="meta empty">还没有专家模板。新建模板后即可安装为专家。</div>';
  $('expertTemplateList').querySelectorAll('[data-expert-template]').forEach((element) => {
    element.onclick = () => selectExpertTemplate(element.dataset.expertTemplate).catch((err) => notify(`模板读取失败：${err.message || err}`, 'error'));
  });
}

function newExpertTemplate({ preserveLists = false } = {}) {
  state.selectedExpertTemplate = null;
  if (!preserveLists) renderExpertTemplates();
  $('expertTemplateEditorTitle').textContent = '新建专家模板';
  $('expertTemplateEditorMeta').textContent = '配置可复用的角色、能力和权限边界';
  $('expertTemplateId').disabled = false;
  $('expertTemplateId').value = '';
  $('expertTemplateName').value = '';
  $('expertTemplateVersion').value = '0.1.0';
  $('expertTemplateVisibility').value = 'organization';
  $('expertTemplateDescription').value = '';
  $('expertTemplateManifest').value = formatJson({
    name: '专业分析专家',
    description: '',
    model: 'deterministic',
    system_prompt: '围绕指定分工独立完成分析，输出可核验的结论、依据和风险提示。',
    skills: [],
    mcp_servers: [],
    permissions: { read_only: true },
  });
  $('expertTemplatePermissions').value = '{}';
  $('expertTemplateEnabled').checked = true;
  $('expertInstallAgentId').value = '';
  $('expertInstallVisibility').value = 'private';
  $('expertInstallPermissions').value = '{}';
  $('deleteExpertTemplateBtn').classList.add('hidden');
  $('saveExpertTemplateBtn').disabled = false;
  $('installExpertTemplateBtn').disabled = true;
}

async function selectExpertTemplate(id, { reload = true } = {}) {
  const item = reload
    ? await api(`/api/expert-templates/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTemplates.find((template) => template.id === id);
  if (!item) return;
  state.selectedExpertTemplate = item;
  const index = state.expertTemplates.findIndex((template) => template.id === item.id);
  if (index >= 0) state.expertTemplates[index] = item;
  renderExpertTemplates();
  const owned = expertOwns(item);
  $('expertTemplateEditorTitle').textContent = item.name;
  $('expertTemplateEditorMeta').textContent = `${item.id} · ${expertVisibilityLabels[item.visibility] || item.visibility}${owned ? '' : ' · 只读模板'}`;
  $('expertTemplateId').disabled = true;
  $('expertTemplateId').value = item.id;
  $('expertTemplateName').value = item.name || '';
  $('expertTemplateVersion').value = item.version || '0.1.0';
  $('expertTemplateVisibility').value = item.visibility || 'organization';
  $('expertTemplateDescription').value = item.description || '';
  $('expertTemplateManifest').value = formatJson(item.manifest || {});
  $('expertTemplatePermissions').value = formatJson(item.permissions || {});
  $('expertTemplateEnabled').checked = !!item.enabled;
  $('deleteExpertTemplateBtn').classList.toggle('hidden', !owned);
  $('saveExpertTemplateBtn').disabled = !owned;
  $('installExpertTemplateBtn').disabled = !item.enabled;
}

function expertTemplatePayload() {
  const scope = expertScopeValues();
  const id = $('expertTemplateId').value.trim();
  const name = $('expertTemplateName').value.trim();
  validateExpertId(id, '模板 ID');
  if (!name) throw new Error('请填写模板名称');
  return {
    id, name,
    description: $('expertTemplateDescription').value.trim(),
    version: $('expertTemplateVersion').value.trim() || '0.1.0',
    source: state.selectedExpertTemplate?.source || 'local',
    manifest: parseExpertJson('expertTemplateManifest', '专家配置'),
    permissions: parseExpertJson('expertTemplatePermissions', '模板权限'),
    visibility: $('expertTemplateVisibility').value,
    enabled: $('expertTemplateEnabled').checked,
    organization_id: scope.organization_id,
    workspace_id: scope.workspace_id,
    owner_user_id: scope.user_id,
  };
}

async function saveExpertTemplate() {
  const button = $('saveExpertTemplateBtn'); setBusy(button, true);
  try {
    const payload = expertTemplatePayload();
    let saved;
    if (state.selectedExpertTemplate) {
      const { id, organization_id, workspace_id, owner_user_id, ...changes } = payload;
      saved = await api(`/api/expert-templates/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'PUT', body: JSON.stringify(changes) });
    } else {
      saved = await api('/api/expert-templates', { method: 'POST', body: JSON.stringify(payload) });
    }
    state.expertTemplates = await api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`);
    await selectExpertTemplate(saved.id, { reload: false });
    notify(`专家模板“${saved.name}”已保存`);
  } catch (err) { notify(`模板保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteExpertTemplate() {
  const item = state.selectedExpertTemplate;
  if (!item || !confirm(`确定删除专家模板“${item.name}”吗？`)) return;
  try {
    await api(`/api/expert-templates/${encodeURIComponent(item.id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertTemplates = await api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`);
    newExpertTemplate();
    notify('专家模板已删除');
  } catch (err) { notify(`模板删除失败：${err.message || err}`, 'error'); }
}

async function installSelectedExpertTemplate() {
  const item = state.selectedExpertTemplate;
  if (!item) return notify('请先选择已保存的专家模板', 'error');
  const button = $('installExpertTemplateBtn'); setBusy(button, true, '安装中…');
  try {
    const scope = expertScopeValues();
    const agentId = $('expertInstallAgentId').value.trim();
    if (agentId) validateExpertId(agentId, 'Agent ID');
    const payload = {
      ...scope,
      agent_id: agentId || null,
      visibility: $('expertInstallVisibility').value,
      permissions: parseExpertJson('expertInstallPermissions', '安装权限'),
      overrides: {},
    };
    const installed = await api(`/api/expert-templates/${encodeURIComponent(item.id)}/install`, { method: 'POST', body: JSON.stringify(payload) });
    [state.expertInstallations, state.agents] = await Promise.all([
      api(`/api/expert-installations?${expertQuery()}`),
      api('/api/agents'),
    ]);
    $('expertInstallAgentId').value = '';
    renderExpertInstallations();
    renderAgents();
    renderAgentsSelect();
    renderLoops();
    refreshExpertTeamAgentSelectors();
    notify(`专家“${installed.agent?.name || installed.agent_id}”已安装`);
  } catch (err) { notify(`专家安装失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

function renderExpertInstallations() {
  $('expertInstallationCount').textContent = String(state.expertInstallations.length);
  $('expertInstallationList').innerHTML = state.expertInstallations.map((item) => {
    const agent = state.agents.find((value) => value.id === item.agent_id);
    const template = state.expertTemplates.find((value) => value.id === item.template_id);
    return `
      <div class="expert-installation-card">
        <div class="expert-avatar">${agentIconSvg(agent || { id: item.agent_id, name: '专家' })}</div>
        <div><strong>${escapeHtml(agent?.name || item.agent_id)}</strong><span>${escapeHtml(template?.name || item.template_id)} · v${escapeHtml(item.installed_version)}</span></div>
        <div class="expert-installation-actions"><button class="text-button" data-open-expert-agent="${escapeHtml(item.agent_id)}" type="button">配置</button><button class="text-button danger-text" data-disable-expert-install="${escapeHtml(item.id)}" type="button">停用</button></div>
      </div>`;
  }).join('') || '<div class="meta empty">尚未安装专家。选择模板后点击“安装当前模板”。</div>';
  $('expertInstallationList').querySelectorAll('[data-open-expert-agent]').forEach((button) => {
    button.onclick = () => { switchTab('agents'); selectAgent(button.dataset.openExpertAgent); };
  });
  $('expertInstallationList').querySelectorAll('[data-disable-expert-install]').forEach((button) => {
    button.onclick = () => disableExpertInstallation(button.dataset.disableExpertInstall);
  });
}

async function disableExpertInstallation(id) {
  const item = state.expertInstallations.find((value) => value.id === id);
  if (!item || !confirm(`确定停用专家“${item.agent_id}”的安装吗？正在使用它的启用团队会阻止此操作。`)) return;
  try {
    await api(`/api/expert-installations/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertInstallations = await api(`/api/expert-installations?${expertQuery()}`);
    renderExpertInstallations();
    refreshExpertTeamAgentSelectors();
    notify('专家安装已停用');
  } catch (err) { notify(`停用失败：${err.message || err}`, 'error'); }
}

function orderedExpertAgents() {
  const installedIds = new Set(state.expertInstallations.map((item) => item.agent_id));
  return [...state.agents].sort((left, right) => Number(installedIds.has(right.id)) - Number(installedIds.has(left.id)) || String(left.name).localeCompare(String(right.name), 'zh-CN'));
}

function expertAgentOptions(selectedId = '') {
  const installedIds = new Set(state.expertInstallations.map((item) => item.agent_id));
  return `<option value="">请选择智能体</option>${orderedExpertAgents().map((agent) => `<option value="${escapeHtml(agent.id)}" ${agent.id === selectedId ? 'selected' : ''}>${escapeHtml(agent.name)}${installedIds.has(agent.id) ? ' · 已安装专家' : ''}</option>`).join('')}`;
}

function refreshExpertTeamAgentSelectors() {
  const supervisor = $('expertTeamSupervisor');
  const supervisorValue = supervisor.value;
  supervisor.innerHTML = expertAgentOptions(supervisorValue);
  if (state.agents.some((agent) => agent.id === supervisorValue)) supervisor.value = supervisorValue;
  $('expertTeamMembers').querySelectorAll('[data-member-agent]').forEach((select) => {
    const value = select.value;
    select.innerHTML = expertAgentOptions(value);
    if (state.agents.some((agent) => agent.id === value)) select.value = value;
  });
}

function renderExpertTeams() {
  $('expertTeamCount').textContent = `${state.expertTeams.length} 个团队`;
  $('expertTeamList').innerHTML = state.expertTeams.map((item) => `
    <button class="card expert-team-card ${state.selectedExpertTeam?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-expert-team="${escapeHtml(item.id)}" type="button">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.description || '未填写团队说明')}</div>
      <div class="expert-card-meta"><span>${(item.members || []).length} 位成员</span><span>主管 ${escapeHtml(item.supervisor_agent_id)}</span></div>
    </button>
  `).join('') || '<div class="meta empty">还没有专家团。请先准备主管和至少两位成员。</div>';
  $('expertTeamList').querySelectorAll('[data-expert-team]').forEach((element) => {
    element.onclick = () => selectExpertTeam(element.dataset.expertTeam).catch((err) => notify(`团队读取失败：${err.message || err}`, 'error'));
  });
  renderWorkbenchTeamOptions();
  renderWorkbenchMode();
}

function defaultExpertTeamMembers() {
  const agents = orderedExpertAgents();
  return [0, 1].map((index) => ({
    id: '',
    agent_id: agents[index]?.id || '',
    role: index === 0 ? '分析专家' : '审查专家',
    member_prompt: index === 0 ? '独立分析目标，给出事实依据、关键结论和待验证事项。' : '从风险、遗漏和可执行性角度独立审查目标并给出建议。',
    permissions: { read_only: true },
  }));
}

function snapshotExpertTeamMembers() {
  return [...$('expertTeamMembers').querySelectorAll('[data-team-member-row]')].map((row) => ({
    id: row.dataset.memberId || '',
    agent_id: row.querySelector('[data-member-agent]').value,
    role: row.querySelector('[data-member-role]').value,
    member_prompt: row.querySelector('[data-member-prompt]').value,
    permissions_text: row.querySelector('[data-member-permissions]').value,
  }));
}

function renderExpertTeamMembers(members) {
  const items = Array.isArray(members) ? members : [];
  $('expertMemberCount').textContent = `${items.length} 位 · 至少 2 位`;
  $('expertTeamMembers').innerHTML = items.map((member, index) => `
    <div class="expert-member-row" data-team-member-row data-member-id="${escapeHtml(member.id || '')}">
      <div class="expert-member-index"><span>${String(index + 1).padStart(2, '0')}</span><strong>${escapeHtml(member.role || '成员')}</strong></div>
      <div class="expert-member-fields">
        <div class="form-grid two">
          <label>成员智能体<select data-member-agent>${expertAgentOptions(member.agent_id || '')}</select></label>
          <label>团队角色<input data-member-role value="${escapeHtml(member.role || '')}" placeholder="例如：数据分析专家" /></label>
        </div>
        <label>成员提示 / 分工<textarea data-member-prompt rows="3" placeholder="说明该成员独立负责的范围和交付要求。">${escapeHtml(member.member_prompt || '')}</textarea></label>
        <label>成员权限 JSON<textarea data-member-permissions class="code" rows="3">${escapeHtml(member.permissions_text ?? formatJson(member.permissions || {}))}</textarea></label>
      </div>
      <button class="icon-button secondary expert-member-remove" data-remove-expert-member="${index}" type="button" title="移除成员" ${items.length <= 2 ? 'disabled' : ''}>×</button>
    </div>
  `).join('');
  $('expertTeamMembers').querySelectorAll('[data-member-role]').forEach((input) => {
    input.oninput = () => { const row = input.closest('[data-team-member-row]'); row.querySelector('.expert-member-index strong').textContent = input.value || '成员'; };
  });
  $('expertTeamMembers').querySelectorAll('[data-remove-expert-member]').forEach((button) => {
    button.onclick = () => {
      const drafts = snapshotExpertTeamMembers();
      drafts.splice(Number(button.dataset.removeExpertMember), 1);
      renderExpertTeamMembers(drafts);
    };
  });
}

function addExpertTeamMember() {
  const drafts = snapshotExpertTeamMembers();
  if (drafts.length >= 20) return notify('一个团队最多配置 20 位成员', 'error');
  const used = new Set(drafts.map((item) => item.agent_id));
  const candidate = orderedExpertAgents().find((agent) => !used.has(agent.id));
  drafts.push({ id: '', agent_id: candidate?.id || '', role: `成员 ${drafts.length + 1}`, member_prompt: '', permissions_text: '{\n  "read_only": true\n}' });
  renderExpertTeamMembers(drafts);
}

function newExpertTeam({ preserveLists = false } = {}) {
  clearExpertRunPolling();
  state.selectedExpertTeam = null;
  state.selectedExpertRun = null;
  state.expertTeamRuns = [];
  if (!preserveLists) renderExpertTeams();
  $('expertTeamEditorTitle').textContent = '新建专家团';
  $('expertTeamEditorMeta').textContent = '主管负责最终汇总与目标验收';
  $('expertTeamId').disabled = false;
  $('expertTeamId').value = '';
  $('expertTeamName').value = '';
  $('expertTeamSupervisor').innerHTML = expertAgentOptions(state.agents.find((agent) => agent.id === 'general-agent')?.id || orderedExpertAgents()[0]?.id || '');
  $('expertTeamDescription').value = '';
  $('expertTeamAggregationPrompt').value = '整合成员独立交付，明确一致结论、分歧、风险和最终建议，并逐项检查验收标准。';
  $('expertTeamAcceptance').value = formatJson(['所有成员分工均有可核验结论', '最终汇总直接回应共同目标']);
  $('expertTeamPermissions').value = '{}';
  $('expertTeamVisibility').value = 'organization';
  $('expertTeamEnabled').checked = true;
  $('deleteExpertTeamBtn').classList.add('hidden');
  $('saveExpertTeamBtn').disabled = false;
  renderExpertTeamMembers(defaultExpertTeamMembers());
  renderExpertRunControls();
  renderExpertRunHistory();
  resetExpertRunLive();
}

async function selectExpertTeam(id, { reload = true, loadRuns = true } = {}) {
  const item = reload
    ? await api(`/api/expert-teams/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTeams.find((team) => team.id === id);
  if (!item) return;
  clearExpertRunPolling();
  state.selectedExpertTeam = item;
  const index = state.expertTeams.findIndex((team) => team.id === item.id);
  if (index >= 0) state.expertTeams[index] = item;
  renderExpertTeams();
  const owned = expertOwns(item);
  $('expertTeamEditorTitle').textContent = item.name;
  $('expertTeamEditorMeta').textContent = `${item.id} · ${expertVisibilityLabels[item.visibility] || item.visibility}${owned ? '' : ' · 只读团队'}`;
  $('expertTeamId').disabled = true;
  $('expertTeamId').value = item.id;
  $('expertTeamName').value = item.name || '';
  $('expertTeamSupervisor').innerHTML = expertAgentOptions(item.supervisor_agent_id);
  $('expertTeamSupervisor').value = item.supervisor_agent_id;
  $('expertTeamVisibility').value = item.visibility || 'organization';
  $('expertTeamDescription').value = item.description || '';
  $('expertTeamAggregationPrompt').value = item.aggregation_prompt || '';
  $('expertTeamAcceptance').value = formatJson(item.acceptance || []);
  $('expertTeamPermissions').value = formatJson(item.permissions || {});
  $('expertTeamEnabled').checked = !!item.enabled;
  $('deleteExpertTeamBtn').classList.toggle('hidden', !owned);
  $('saveExpertTeamBtn').disabled = !owned;
  renderExpertTeamMembers(item.members || []);
  renderExpertRunControls();
  if (loadRuns) await loadExpertTeamRuns(item.id, { preserveSelection: true });
}

function expertTeamMembersPayload() {
  const drafts = snapshotExpertTeamMembers();
  if (drafts.length < 2) throw new Error('专家团至少需要两位成员');
  const members = drafts.map((member, index) => {
    if (!member.agent_id) throw new Error(`请选择第 ${index + 1} 位成员的智能体`);
    if (!member.role.trim()) throw new Error(`请填写第 ${index + 1} 位成员的团队角色`);
    let permissions;
    try { permissions = JSON.parse(member.permissions_text.trim() || '{}'); }
    catch (_) { throw new Error(`第 ${index + 1} 位成员的权限不是有效 JSON`); }
    if (!permissions || Array.isArray(permissions) || typeof permissions !== 'object') throw new Error(`第 ${index + 1} 位成员的权限必须是 JSON 对象`);
    return {
      ...(member.id ? { id: member.id } : {}),
      agent_id: member.agent_id,
      role: member.role.trim(),
      execution_mode: 'parallel',
      depends_on: [],
      member_prompt: member.member_prompt.trim(),
      position: index,
      permissions,
    };
  });
  if (new Set(members.map((member) => member.agent_id)).size !== members.length) throw new Error('同一个智能体不能作为多个并行成员');
  return members;
}

function expertTeamPayload() {
  const scope = expertScopeValues();
  const id = $('expertTeamId').value.trim();
  const name = $('expertTeamName').value.trim();
  validateExpertId(id, '团队 ID');
  if (!name) throw new Error('请填写团队名称');
  if (!$('expertTeamSupervisor').value) throw new Error('请选择主管智能体');
  return {
    id, name,
    description: $('expertTeamDescription').value.trim(),
    supervisor_agent_id: $('expertTeamSupervisor').value,
    aggregation_prompt: $('expertTeamAggregationPrompt').value.trim(),
    acceptance: parseExpertJson('expertTeamAcceptance', '验收标准', 'array'),
    budget: state.selectedExpertTeam?.budget || {},
    members: expertTeamMembersPayload(),
    organization_id: scope.organization_id,
    workspace_id: scope.workspace_id,
    owner_user_id: scope.user_id,
    visibility: $('expertTeamVisibility').value,
    permissions: parseExpertJson('expertTeamPermissions', '团队权限'),
    enabled: $('expertTeamEnabled').checked,
  };
}

async function saveExpertTeam() {
  const button = $('saveExpertTeamBtn'); setBusy(button, true);
  try {
    const payload = expertTeamPayload();
    let saved;
    if (state.selectedExpertTeam) {
      const { id, organization_id, workspace_id, owner_user_id, ...changes } = payload;
      saved = await api(`/api/expert-teams/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'PUT', body: JSON.stringify(changes) });
    } else {
      saved = await api('/api/expert-teams', { method: 'POST', body: JSON.stringify(payload) });
    }
    state.expertTeams = await api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`);
    await selectExpertTeam(saved.id, { reload: false, loadRuns: true });
    notify(`专家团“${saved.name}”已保存`);
  } catch (err) { notify(`团队保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteExpertTeam() {
  const item = state.selectedExpertTeam;
  if (!item || !confirm(`确定删除专家团“${item.name}”吗？已有运行审计记录的团队不能删除，可取消“启用团队”后保存。`)) return;
  try {
    await api(`/api/expert-teams/${encodeURIComponent(item.id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertTeams = await api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`);
    newExpertTeam();
    notify('专家团已删除');
  } catch (err) { notify(`团队删除失败：${err.message || err}`, 'error'); }
}

function renderExpertRunModelOptions() {
  const select = $('expertRunModel');
  const previous = select.value || readPreference('model') || 'deterministic';
  const models = state.models.filter((model) => model.enabled);
  select.innerHTML = models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join('');
  if (models.some((model) => model.id === previous)) select.value = previous;
  else if (models.length) select.value = models[0].id;
}

function renderExpertRunControls() {
  const team = state.selectedExpertTeam;
  $('expertRunTeamName').value = team ? team.name : '尚未选择';
  $('runExpertTeamBtn').disabled = !team || !team.enabled;
  $('expertRunLiveMeta').textContent = team ? `${team.members?.length || 0} 位成员 · ${team.enabled ? '可运行' : '已停用'}` : '等待选择团队';
  renderExpertRunModelOptions();
}

function expertRunStatusLabel(status) {
  return expertRunStatusLabels[status] || status || '未知';
}

function expertRunIsActive(status) {
  return ['queued', 'running', 'aggregating'].includes(status);
}

function latestExpertMemberRuns(memberRuns) {
  const latest = new Map();
  for (const item of memberRuns || []) {
    const current = latest.get(item.member_id);
    if (!current || Number(item.attempt || 0) >= Number(current.attempt || 0)) latest.set(item.member_id, item);
  }
  return [...latest.values()];
}

function renderExpertRunHistory() {
  $('expertRunCount').textContent = String(state.expertTeamRuns.length);
  $('expertRunList').innerHTML = state.expertTeamRuns.map((run) => {
    const latest = latestExpertMemberRuns(run.member_runs);
    const completed = latest.filter((member) => member.status === 'completed').length;
    return `
      <button class="expert-run-history-item ${state.selectedExpertRun?.id === run.id ? 'active' : ''}" data-expert-run="${escapeHtml(run.id)}" type="button">
        <span class="expert-run-dot ${runtimeStatusClass(run.status)}"></span>
        <span><strong>${escapeHtml(expertRunStatusLabel(run.status))}</strong><small>${completed}/${latest.length || state.selectedExpertTeam?.members?.length || 0} 位成员完成 · ${escapeHtml(runtimeTime(run.created_at))}</small></span>
        <small>查看 →</small>
      </button>`;
  }).join('') || `<div class="meta empty">${state.selectedExpertTeam ? '该团队还没有运行记录。' : '选择团队后查看运行历史。'}</div>`;
  $('expertRunList').querySelectorAll('[data-expert-run]').forEach((button) => {
    button.onclick = () => selectExpertRun(button.dataset.expertRun).catch((err) => notify(`运行读取失败：${err.message || err}`, 'error'));
  });
}

function resetExpertRunLive(message = '选择并保存团队，然后提交一个共同目标。') {
  $('expertRunLive').innerHTML = `<div class="expert-empty-state"><strong>尚未开始运行</strong><span>${escapeHtml(message)}</span></div>`;
}

function renderExpertRunLive() {
  const run = state.selectedExpertRun;
  if (!run) return resetExpertRunLive();
  const team = state.selectedExpertTeam;
  const latest = latestExpertMemberRuns(run.member_runs);
  const completed = latest.filter((member) => member.status === 'completed').length;
  const total = latest.length || team?.members?.length || 0;
  const progress = total ? Math.round((completed / total) * 100) : 0;
  const retryAllowed = ['partial_failed', 'failed'].includes(run.status);
  const supervisor = run.result?.supervisor || {};
  const summary = run.result?.summary || supervisor.summary || '';
  const runError = run.error?.message || '';
  $('expertRunLiveMeta').textContent = expertRunIsActive(run.status) ? '每秒刷新运行状态' : `${expertRunStatusLabel(run.status)} · ${runtimeTime(run.updated_at || run.finished_at)}`;
  $('expertRunLive').innerHTML = `
    <div class="expert-run-live-header">
      <div><span class="status ${runtimeStatusClass(run.status)}">${escapeHtml(expertRunStatusLabel(run.status))}</span><h2>${escapeHtml(team?.name || run.team_id)}</h2><p>${escapeHtml(run.id)} · 创建于 ${escapeHtml(runtimeTime(run.created_at))}</p></div>
      ${run.parent_task_id ? `<button class="secondary" data-team-parent-task="${escapeHtml(run.parent_task_id)}" type="button">查看总任务</button>` : ''}
    </div>
    <div class="expert-progress"><div><span>成员进度</span><strong>${completed}/${total}</strong></div><i><b style="width:${progress}%"></b></i></div>
    <div class="expert-live-section">
      <div class="expert-live-section-title"><h3>并行成员</h3><span>每位成员使用独立上下文</span></div>
      <div class="expert-member-run-grid">
        ${latest.map((memberRun) => {
          const member = (team?.members || []).find((value) => value.id === memberRun.member_id) || {};
          const delivery = memberRun.output?.summary || '';
          const error = memberRun.error?.message || '';
          const canRetry = retryAllowed && ['failed', 'cancelled'].includes(memberRun.status);
          return `<article class="expert-member-run ${runtimeStatusClass(memberRun.status)}">
            <div class="expert-member-run-head"><div><strong>${escapeHtml(member.role || memberRun.input?.role || memberRun.member_id)}</strong><span>第 ${Number(memberRun.attempt || 1)} 次运行</span></div><span class="status ${runtimeStatusClass(memberRun.status)}">${escapeHtml(expertRunStatusLabel(memberRun.status))}</span></div>
            <div class="expert-child-task"><span>子任务</span><code>${escapeHtml(memberRun.child_task_id || '等待创建')}</code>${memberRun.child_task_id ? `<button class="text-button" data-team-child-task="${escapeHtml(memberRun.child_task_id)}" type="button">打开</button>` : ''}</div>
            ${delivery ? `<div class="expert-delivery"><strong>交付摘要</strong>${renderMarkdown(delivery)}</div>` : error ? `<div class="expert-member-error">${escapeHtml(error)}</div>` : '<div class="expert-member-waiting">正在等待成员交付…</div>'}
            ${canRetry ? `<button class="secondary expert-retry-member" data-retry-team-member="${escapeHtml(memberRun.id)}" type="button">仅重试此成员</button>` : ''}
          </article>`;
        }).join('') || '<div class="meta empty">成员任务正在创建，请稍候。</div>'}
      </div>
    </div>
    <div class="expert-live-section supervisor-summary">
      <div class="expert-live-section-title"><h3>主管汇总</h3><span>${run.supervisor_child_task_id ? '主管任务已创建' : expertRunIsActive(run.status) ? '成员完成后开始' : '暂无主管交付'}</span></div>
      ${summary ? `<div class="expert-supervisor-answer">${renderMarkdown(summary)}</div>` : `<div class="expert-supervisor-placeholder">${runError ? escapeHtml(runError) : run.status === 'aggregating' ? '主管正在整合成员结论并执行最终验收…' : run.status === 'partial_failed' ? '失败成员完成独立重试后，主管会重新汇总。' : '等待全部成员完成。'}</div>`}
      ${run.supervisor_child_task_id ? `<button class="text-button" data-team-child-task="${escapeHtml(run.supervisor_child_task_id)}" type="button">查看主管任务</button>` : ''}
    </div>`;
  $('expertRunLive').querySelectorAll('[data-team-child-task], [data-team-parent-task]').forEach((button) => {
    button.onclick = () => openTask(button.dataset.teamChildTask || button.dataset.teamParentTask);
  });
  $('expertRunLive').querySelectorAll('[data-retry-team-member]').forEach((button) => {
    button.onclick = () => retryExpertTeamMember(button.dataset.retryTeamMember, button);
  });
}

function updateExpertRunState(run) {
  state.selectedExpertRun = run;
  const index = state.expertTeamRuns.findIndex((item) => item.id === run.id);
  if (index >= 0) state.expertTeamRuns[index] = run;
  else state.expertTeamRuns.unshift(run);
  renderExpertRunHistory();
  renderExpertRunLive();
}

function clearExpertRunPolling() {
  if (state.expertRunPollTimer) clearTimeout(state.expertRunPollTimer);
  state.expertRunPollTimer = null;
  state.expertRunPollToken += 1;
}

function startExpertRunPolling(runId, { graceMs = 0 } = {}) {
  clearExpertRunPolling();
  const token = state.expertRunPollToken;
  const graceUntil = Date.now() + graceMs;
  let sawActive = false;
  const poll = async () => {
    if (token !== state.expertRunPollToken || state.selectedExpertRun?.id !== runId) return;
    try {
      const run = await api(`/api/expert-team-runs/${encodeURIComponent(runId)}?${expertQuery()}`);
      if (token !== state.expertRunPollToken || state.selectedExpertRun?.id !== runId) return;
      updateExpertRunState(run);
      if (expertRunIsActive(run.status)) sawActive = true;
      if (expertRunIsActive(run.status) || (!sawActive && Date.now() < graceUntil)) {
        state.expertRunPollTimer = setTimeout(poll, 900);
      }
    } catch (_) {
      if (token !== state.expertRunPollToken) return;
      $('expertRunLiveMeta').textContent = '状态刷新暂时中断，正在重试';
      if (expertRunIsActive(state.selectedExpertRun?.status) || Date.now() < graceUntil) state.expertRunPollTimer = setTimeout(poll, 1500);
    }
  };
  state.expertRunPollTimer = setTimeout(poll, 150);
}

async function loadExpertTeamRuns(teamId, { preserveSelection = false } = {}) {
  const selectedRunId = preserveSelection ? state.selectedExpertRun?.id : '';
  state.expertTeamRuns = await api(`/api/expert-teams/${encodeURIComponent(teamId)}/runs?${expertQuery()}`);
  const candidate = state.expertTeamRuns.find((run) => run.id === selectedRunId) || state.expertTeamRuns[0];
  if (candidate) await selectExpertRun(candidate.id, { reload: true });
  else {
    clearExpertRunPolling();
    state.selectedExpertRun = null;
    renderExpertRunHistory();
    resetExpertRunLive('该团队还没有运行记录，请提交一个共同目标。');
  }
}

async function selectExpertRun(id, { reload = true } = {}) {
  const run = reload
    ? await api(`/api/expert-team-runs/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTeamRuns.find((item) => item.id === id);
  if (!run) return;
  clearExpertRunPolling();
  updateExpertRunState(run);
  if (expertRunIsActive(run.status)) startExpertRunPolling(run.id);
}

async function runSelectedExpertTeam() {
  const team = state.selectedExpertTeam;
  if (!team) return notify('请先选择并保存专家团', 'error');
  const message = $('expertRunGoal').value.trim();
  if (!message) return notify('请填写团队共同目标', 'error');
  const button = $('runExpertTeamBtn'); setBusy(button, true, '提交中…');
  try {
    const payload = {
      message,
      model_id: $('expertRunModel').value || null,
      conversation_id: createConversationId(),
      ...expertScopeValues(),
    };
    const accepted = await api(`/api/expert-teams/${encodeURIComponent(team.id)}/runs`, { method: 'POST', body: JSON.stringify(payload) });
    state.selectedExpertRun = accepted.team_run;
    state.expertTeamRuns = [accepted.team_run, ...state.expertTeamRuns.filter((run) => run.id !== accepted.team_run.id)];
    updateExpertRunState(accepted.team_run);
    startExpertRunPolling(accepted.team_run.id);
    notify('团队任务已提交，成员将并行执行');
  } catch (err) { notify(`团队运行提交失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function retryExpertTeamMember(memberRunId, button) {
  const run = state.selectedExpertRun;
  if (!run) return;
  setBusy(button, true, '提交中…');
  try {
    await api(`/api/expert-team-runs/${encodeURIComponent(run.id)}/members/${encodeURIComponent(memberRunId)}/retry?${expertQuery()}`, {
      method: 'POST', body: JSON.stringify({ note: '从专家团运行页面独立重试失败成员' }),
    });
    $('expertRunLiveMeta').textContent = '成员重试已提交，正在刷新';
    startExpertRunPolling(run.id, { graceMs: 5000 });
    notify('失败成员已单独提交重试，其他成员结果会保留');
  } catch (err) {
    notify(`成员重试失败：${err.message || err}`, 'error');
    setBusy(button, false);
  }
}

const memoryScopeLabels = { organization: '当前实例', workspace: '当前工作区', user: '本机使用者', agent: '智能体', conversation: '对话' };

function memoryScopeValues() {
  return {
    organization_id: 'local-org',
    workspace_id: 'default',
    user_id: 'local-user',
    agent_id: $('agentSelect')?.value || readPreference('agent') || 'general-agent',
    conversation_id: state.conversationId,
  };
}

function memoryQuery() {
  return new URLSearchParams(memoryScopeValues()).toString();
}

async function loadMemoriesOnly({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection ? state.selectedMemory?.id : '';
  const [memories] = await Promise.all([
    api(`/api/memories?${memoryQuery()}`),
    loadConversationSummaries({ preserveSelection: true }),
  ]);
  state.memories = memories;
  state.selectedMemory = selectedId ? state.memories.find((item) => item.id === selectedId) || null : state.selectedMemory;
  renderMemories();
  await loadEffectiveMemoryContext();
  if (state.selectedMemory) await selectMemory(state.selectedMemory.id, { reload: false });
}

function conversationSummaryScopeQuery() {
  const { organization_id, workspace_id, user_id } = memoryScopeValues();
  return new URLSearchParams({ organization_id, workspace_id, user_id }).toString();
}

function renderConversationSummaryEditor() {
  const items = state.conversationSummaries;
  const currentId = state.conversationId;
  const selectedId = state.selectedConversationSummary?.conversation_id || currentId;
  const options = items.map((item) => item.conversation_id);
  if (!options.includes(currentId)) options.unshift(currentId);
  $('conversationSummarySelect').innerHTML = options.map((id) => {
    const item = items.find((value) => value.conversation_id === id);
    const label = id === currentId ? `当前对话 · ${id}` : `${id}${item ? ` · v${item.version}` : ''}`;
    return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
  }).join('');
  $('conversationSummarySelect').value = options.includes(selectedId) ? selectedId : currentId;
  const item = items.find((value) => value.conversation_id === $('conversationSummarySelect').value) || null;
  state.selectedConversationSummary = item;
  $('conversationSummaryContent').value = item?.summary || '';
  $('conversationSummaryConstraints').value = (item?.preserved_constraints || []).join('\n');
  $('conversationSummaryThroughTask').value = item?.through_task_id || '';
  $('conversationSummaryMeta').textContent = item
    ? `版本 ${item.version} · 约 ${item.token_count || 0} tokens · 更新于 ${runtimeTime(item.updated_at)}`
    : `“${$('conversationSummarySelect').value}”尚无摘要，可人工创建或等待平台自动压缩`;
  $('deleteConversationSummaryBtn').disabled = !item;
  $('saveConversationSummaryBtn').disabled = false;
}

async function loadConversationSummaries({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection
    ? state.selectedConversationSummary?.conversation_id || state.conversationId
    : state.conversationId;
  const response = await api(`/api/conversation-summaries?${conversationSummaryScopeQuery()}`);
  state.conversationSummaries = Array.isArray(response) ? response : [];
  state.selectedConversationSummary = state.conversationSummaries.find((item) => item.conversation_id === selectedId) || null;
  renderConversationSummaryEditor();
}

function selectConversationSummary(conversationId) {
  state.selectedConversationSummary = state.conversationSummaries.find((item) => item.conversation_id === conversationId) || null;
  renderConversationSummaryEditor();
  $('conversationSummarySelect').value = conversationId;
}

async function saveConversationSummary() {
  const conversationId = $('conversationSummarySelect').value || state.conversationId;
  const summary = $('conversationSummaryContent').value.trim();
  if (!summary) return notify('请填写对话摘要内容', 'error');
  const button = $('saveConversationSummaryBtn'); setBusy(button, true);
  try {
    const preservedConstraints = $('conversationSummaryConstraints').value
      .split('\n').map((item) => item.trim()).filter(Boolean);
    const existing = state.conversationSummaries.find((item) => item.conversation_id === conversationId);
    const saved = await api(`/api/conversation-summaries/${encodeURIComponent(conversationId)}?${conversationSummaryScopeQuery()}`, {
      method: 'PUT',
      body: JSON.stringify({
        summary,
        preserved_constraints: preservedConstraints,
        through_task_id: existing?.through_task_id || '',
        model_id: 'manual-editor',
      }),
    });
    const index = state.conversationSummaries.findIndex((item) => item.conversation_id === conversationId);
    if (index >= 0) state.conversationSummaries[index] = saved;
    else state.conversationSummaries.unshift(saved);
    state.selectedConversationSummary = saved;
    renderConversationSummaryEditor();
    $('conversationSummarySelect').value = conversationId;
    notify('对话摘要已保存，后续任务会把它作为较早背景使用');
  } catch (err) { notify(`摘要保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteConversationSummary() {
  const item = state.selectedConversationSummary;
  if (!item || !confirm(`确定删除对话“${item.conversation_id}”的摘要吗？原始任务记录不会删除。`)) return;
  const button = $('deleteConversationSummaryBtn'); setBusy(button, true, '删除中…');
  try {
    await api(`/api/conversation-summaries/${encodeURIComponent(item.conversation_id)}?${conversationSummaryScopeQuery()}`, { method: 'DELETE' });
    state.conversationSummaries = state.conversationSummaries.filter((value) => value.conversation_id !== item.conversation_id);
    state.selectedConversationSummary = null;
    renderConversationSummaryEditor();
    notify('对话摘要已删除，原始任务记录仍然保留');
  } catch (err) { notify(`摘要删除失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

function renderMemories() {
  const scopeFilter = $('memoryScopeFilter')?.value || '';
  const statusFilter = $('memoryStatusFilter')?.value || 'all';
  const items = state.memories.filter((item) => {
    if (scopeFilter && item.scope_type !== scopeFilter) return false;
    if (statusFilter === 'enabled' && !item.enabled) return false;
    if (statusFilter === 'disabled' && item.enabled) return false;
    return true;
  });
  $('memoryCount').textContent = String(items.length);
  $('memoryList').innerHTML = items.map((item) => `
    <div class="card memory-card ${state.selectedMemory?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-memory="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.title || item.content.slice(0, 32))}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.content)}</div>
      <div class="memory-card-meta"><span>${escapeHtml(memoryScopeLabels[item.scope_type] || item.scope_type)}</span><span>${escapeHtml(item.kind)}</span><span>信任 ${item.trust_level}</span></div>
    </div>
  `).join('') || '<div class="meta empty">当前筛选条件下没有记忆。</div>';
  document.querySelectorAll('[data-memory]').forEach((element) => { element.onclick = () => selectMemory(element.dataset.memory); });
}

function memoryScopeId(scopeType) {
  const values = memoryScopeValues();
  return ({ organization: values.organization_id, workspace: values.workspace_id, user: values.user_id, agent: values.agent_id, conversation: values.conversation_id })[scopeType] || '';
}

function syncMemoryScopeId() {
  $('memoryScopeId').value = memoryScopeId($('memoryScopeType').value);
}

function newMemory() {
  state.selectedMemory = null;
  renderMemories();
  $('memoryEditorTitle').textContent = '新建记忆';
  $('memoryId').value = '';
  $('memoryTitle').value = '';
  $('memoryKind').value = 'preference';
  $('memoryScopeType').disabled = false;
  $('memoryScopeType').value = 'user';
  syncMemoryScopeId();
  $('memoryTrust').value = '80';
  $('memoryExpiresAt').value = '';
  $('memoryContent').value = '';
  $('memoryTags').value = '';
  $('memoryEnabled').checked = true;
  $('deleteMemoryBtn').classList.add('hidden');
  $('memoryRevisionMeta').textContent = '保存后自动记录修订';
  $('memoryRevisions').innerHTML = '<div class="meta empty">选择已有记忆后查看修订记录</div>';
}

async function selectMemory(id, { reload = true } = {}) {
  if (reload) state.memories = await api(`/api/memories?${memoryQuery()}`);
  const item = state.memories.find((memory) => memory.id === id);
  if (!item) return;
  state.selectedMemory = item;
  renderMemories();
  $('memoryEditorTitle').textContent = item.title || '未命名记忆';
  $('memoryId').value = item.id;
  $('memoryTitle').value = item.title || '';
  $('memoryKind').value = item.kind || 'preference';
  $('memoryScopeType').value = item.scope_type;
  $('memoryScopeType').disabled = true;
  $('memoryScopeId').value = item.scope_id;
  $('memoryTrust').value = String(item.trust_level ?? 80);
  $('memoryExpiresAt').value = item.expires_at ? item.expires_at.slice(0, 16) : '';
  $('memoryContent').value = item.content || '';
  $('memoryTags').value = (item.tags || []).join(', ');
  $('memoryEnabled').checked = !!item.enabled;
  $('deleteMemoryBtn').classList.remove('hidden');
  const revisions = await api(`/api/memories/${encodeURIComponent(id)}/revisions?${memoryQuery()}`);
  $('memoryRevisionMeta').textContent = `${revisions.length} 个修订 · 最近更新 ${runtimeTime(item.updated_at)}`;
  $('memoryRevisions').innerHTML = revisions.slice().reverse().map((revision) => `
    <div class="memory-revision"><strong>版本 ${revision.revision}</strong><span>${escapeHtml(revision.reason)} · ${escapeHtml(runtimeTime(revision.created_at))}</span></div>
  `).join('') || '<div class="meta empty">暂无修订记录</div>';
}

function memoryPayload() {
  const scope = memoryScopeValues();
  return {
    ...scope,
    scope_type: $('memoryScopeType').value,
    kind: $('memoryKind').value,
    title: $('memoryTitle').value.trim(),
    content: $('memoryContent').value.trim(),
    tags: $('memoryTags').value.split(',').map((item) => item.trim()).filter(Boolean),
    trust_level: Number($('memoryTrust').value),
    enabled: $('memoryEnabled').checked,
    expires_at: $('memoryExpiresAt').value ? new Date($('memoryExpiresAt').value).toISOString() : null,
  };
}

async function saveMemory() {
  const button = $('saveMemoryBtn'); setBusy(button, true);
  try {
    const payload = memoryPayload();
    if (!payload.content) throw new Error('请填写要记住的内容');
    let saved;
    if (state.selectedMemory) {
      const { scope_type, organization_id, workspace_id, user_id, agent_id, conversation_id, ...changes } = payload;
      saved = await api(`/api/memories/${encodeURIComponent(state.selectedMemory.id)}?${memoryQuery()}`, { method: 'PUT', body: JSON.stringify({ ...changes, reason: 'ui_update' }) });
    } else {
      saved = await api('/api/memories', { method: 'POST', body: JSON.stringify(payload) });
    }
    await loadMemoriesOnly({ preserveSelection: false });
    await selectMemory(saved.id, { reload: false });
    notify(`记忆“${saved.title || saved.id}”已保存`);
  } catch (err) { notify(`记忆保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteMemory() {
  const item = state.selectedMemory;
  if (!item || !confirm(`确定删除记忆“${item.title || item.content.slice(0, 24)}”吗？修订审计会保留。`)) return;
  await api(`/api/memories/${encodeURIComponent(item.id)}?${memoryQuery()}`, { method: 'DELETE' });
  state.selectedMemory = null;
  await loadMemoriesOnly();
  newMemory();
  notify('记忆已删除，不会再注入后续任务');
}

async function loadEffectiveMemoryContext() {
  const effective = await api(`/api/context/effective?${memoryQuery()}`);
  $('memoryEffectiveContext').textContent = effective.effective_context || '当前没有生效的长期记忆。';
  $('memoryEffectiveMeta').textContent = `当前任务将使用 ${effective.used_memory_ids?.length || 0} 条记忆 · ${memoryScopeValues().agent_id}`;
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function safeArtifactDownloadUrl(value, { inline = false } = {}) {
  try {
    const url = new URL(String(value || ''), window.location.href);
    if (url.origin !== window.location.origin || !/^\/api\/artifacts\/[^/]+\/download$/.test(url.pathname)) return '';
    return inline && url.searchParams.get('inline') === 'true' ? `${url.pathname}?inline=true` : url.pathname;
  } catch (_) {
    return '';
  }
}

function resetArtifactPreview(message = '从左侧选择一个文件。') {
  $('artifactPreviewTitle').textContent = '选择文件预览';
  $('artifactPreviewMeta').textContent = '支持 Word、PDF、PPT、Excel、Markdown 和 HTML';
  const download = $('artifactPreviewDownload');
  download.removeAttribute('href');
  download.classList.add('hidden');
  $('artifactPreview').className = 'artifact-preview-empty';
  $('artifactPreview').textContent = message;
}

async function loadArtifactsOnly({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection ? state.selectedArtifact?.id : '';
  const kind = $('artifactKindFilter')?.value || '';
  const query = new URLSearchParams({ workspace_id: 'default', limit: '300' });
  if (kind) query.set('kind', kind);
  state.artifacts = runtimeArray(await api(`/api/artifacts?${query.toString()}`));
  state.selectedArtifact = selectedId ? state.artifacts.find((item) => item.id === selectedId) || null : null;
  renderArtifactWorkspace();
  if (state.selectedArtifact) await selectArtifact(state.selectedArtifact.id, { reload: false });
  else resetArtifactPreview();
}

function renderArtifactWorkspace() {
  $('artifactCount').textContent = String(state.artifacts.length);
  $('artifactWorkspaceList').innerHTML = state.artifacts.map((item) => {
    const kind = String(item.kind || 'file');
    const version = item.version ? `v${String(item.version)} · ` : '';
    const metadata = `${String(item.task_id || '手动生成')} · ${version}${formatBytes(item.size)}`;
    const hash = item.sha256 ? `SHA-256 ${String(item.sha256).slice(0, 12)}…` : '暂无校验值';
    return `
      <button class="artifact-workspace-item ${state.selectedArtifact?.id === item.id ? 'active' : ''}" data-workspace-artifact="${escapeHtml(item.id)}" type="button">
        <span class="artifact-file-icon">${escapeHtml(kind.slice(0, 4).toUpperCase())}</span>
        <span class="artifact-file-copy"><strong>${escapeHtml(item.name || '未命名文件')}</strong><small>${escapeHtml(metadata)}</small><small class="artifact-file-hash">${escapeHtml(hash)}</small></span>
        <span class="artifact-file-time">${escapeHtml(runtimeTime(item.created_at))}</span>
      </button>
    `;
  }).join('') || '<div class="meta empty">当前工作区还没有生成文件。</div>';
  $('artifactWorkspaceList').querySelectorAll('[data-workspace-artifact]').forEach((element) => {
    element.onclick = () => selectArtifact(element.dataset.workspaceArtifact).catch((err) => notify(`产物预览失败：${err.message || err}`, 'error'));
  });
}

async function openArtifactPreview(id) {
  if (!id) return;
  switchTab('artifacts');
  if (!state.artifacts.some((item) => item.id === id)) await loadArtifactsOnly();
  await selectArtifact(id, { reload: false });
}

async function selectArtifact(id, { reload = true } = {}) {
  if (reload && !state.artifacts.some((item) => item.id === id)) await loadArtifactsOnly();
  const item = state.artifacts.find((artifact) => artifact.id === id) || await api(`/api/artifacts/${encodeURIComponent(id)}`);
  state.selectedArtifact = item;
  renderArtifactWorkspace();
  $('artifactPreviewTitle').textContent = item.name;
  $('artifactPreviewMeta').textContent = `${String(item.kind || '').toUpperCase()} · ${formatBytes(item.size)} · v${item.version || 1}${item.sha256 ? ` · SHA-256 ${item.sha256.slice(0, 12)}…` : ''}`;
  const download = $('artifactPreviewDownload');
  const downloadUrl = safeArtifactDownloadUrl(item.download_url);
  if (downloadUrl) {
    download.href = downloadUrl;
    download.classList.remove('hidden');
  } else {
    download.removeAttribute('href');
    download.classList.add('hidden');
  }
  $('artifactPreview').className = 'artifact-preview-loading';
  $('artifactPreview').textContent = '正在生成受控预览…';
  try {
    const preview = await api(`/api/artifacts/${encodeURIComponent(id)}/preview`);
    renderArtifactPreview(preview);
  } catch (err) {
    $('artifactPreview').className = 'artifact-preview-error';
    $('artifactPreview').textContent = `预览生成失败：${err.message || err}`;
    throw err;
  }
}

function previewTable(rows) {
  if (!rows?.length) return '<div class="meta empty">空表格</div>';
  return `<div class="artifact-table-wrap"><table>${rows.map((row, index) => `<tr>${(row || []).map((cell) => `<${index === 0 ? 'th' : 'td'}>${escapeHtml(cell)}</${index === 0 ? 'th' : 'td'}>`).join('')}</tr>`).join('')}</table></div>`;
}

function renderArtifactPreview(preview) {
  const box = $('artifactPreview');
  const previewKind = ['markdown', 'html', 'pdf', 'spreadsheet', 'document', 'slides', 'text'].includes(preview?.preview_kind) ? preview.preview_kind : 'unknown';
  box.className = `artifact-preview ${previewKind}`;
  if (previewKind === 'markdown') {
    box.innerHTML = `<div class="artifact-markdown">${renderMarkdown(preview.content || '')}</div>`;
    return;
  }
  if (previewKind === 'html') {
    const frame = document.createElement('iframe');
    frame.className = 'artifact-frame';
    frame.title = 'HTML 文件预览';
    frame.setAttribute('sandbox', '');
    frame.referrerPolicy = 'no-referrer';
    frame.srcdoc = String(preview.content || '');
    box.replaceChildren(frame);
    return;
  }
  if (previewKind === 'pdf') {
    const source = safeArtifactDownloadUrl(preview.url, { inline: true });
    if (!source) {
      box.innerHTML = '<div class="artifact-preview-empty">PDF 预览地址无效，可下载原文件查看。</div>';
      return;
    }
    const frame = document.createElement('iframe');
    frame.className = 'artifact-frame pdf';
    frame.title = 'PDF 文件预览';
    frame.setAttribute('sandbox', '');
    frame.referrerPolicy = 'no-referrer';
    frame.src = source;
    box.replaceChildren(frame);
    return;
  }
  if (previewKind === 'spreadsheet') {
    box.innerHTML = (preview.sheets || []).map((sheet) => `<section class="artifact-sheet"><h3>${escapeHtml(sheet.name)}</h3>${previewTable(sheet.rows)}</section>`).join('') || '<div class="artifact-preview-empty">工作簿中没有可预览的数据。</div>';
    return;
  }
  if (previewKind === 'document') {
    const paragraphs = (preview.paragraphs || []).map((item) => `<p>${escapeHtml(item)}</p>`).join('');
    const tables = (preview.tables || []).map(previewTable).join('');
    box.innerHTML = paragraphs || tables ? `<article class="artifact-document">${paragraphs}${tables}</article>` : '<div class="artifact-preview-empty">文档中没有可提取的文字或表格。</div>';
    return;
  }
  if (previewKind === 'slides') {
    const slides = (preview.slides || []).map((slide) => `<section><span>${escapeHtml(slide.number)}</span><h3>${escapeHtml(slide.title)}</h3>${(slide.texts || []).slice(1).map((text) => `<p>${escapeHtml(text)}</p>`).join('')}</section>`).join('');
    box.innerHTML = slides ? `<div class="artifact-slides">${slides}</div>` : '<div class="artifact-preview-empty">演示文稿中没有可预览的页面。</div>';
    return;
  }
  if (previewKind === 'text') {
    box.innerHTML = `<pre>${escapeHtml(preview.content || '')}</pre>`;
    return;
  }
  box.innerHTML = `<div class="artifact-preview-empty">${escapeHtml(preview.message || '该格式暂不支持预览，可下载原文件。')}</div>`;
}

function renderCapabilities() {
  const capability = state.capabilities || {};
  const status = (enabled, configured = true) => {
    if (!configured) return { label: '待配置', className: 'pending' };
    return enabled ? { label: '可使用', className: 'ready' } : { label: '已关闭', className: 'off' };
  };
  const fileUpload = capability.file_upload || {};
  const webSearch = capability.web_search || {};
  const localMcp = capability.stdio_mcp || {};
  const remoteMcp = capability.remote_mcp || {};
  const documentCapability = capability.document_output || {};
  const documents = Array.isArray(documentCapability)
    ? documentCapability
    : Array.isArray(documentCapability.formats) ? documentCapability.formats : [];
  const pptxReady = Array.isArray(documentCapability)
    ? documentCapability.includes('pptx')
    : !!documentCapability.pptx_configured;
  const memory = capability.memory || {};
  const experts = capability.expert_teams || {};
  const automation = capability.automation || {};
  const cards = [
    {
      title: '文件与资料',
      detail: fileUpload.supported
        ? `单文件最大 ${fileUpload.max_mb || 20} MB · 支持 ${Array.isArray(fileUpload.text_extraction) ? fileUpload.text_extraction.map((item) => String(item).toUpperCase()).join(' / ') : '常见文档'} 正文提取`
        : '当前版本不支持文件上传',
      state: status(!!fileUpload.supported),
    },
    {
      title: '联网搜索',
      detail: webSearch.configured ? (webSearch.enabled ? '已配置并允许任务调用' : '服务已配置，可在能力开关中启用') : '需要先配置搜索服务',
      state: status(!!webSearch.enabled, !!webSearch.configured),
    },
    {
      title: '本地 MCP',
      detail: localMcp.enabled ? '允许启动白名单内的本地工具进程' : '支持接入，默认关闭进程执行',
      state: status(!!localMcp.enabled),
    },
    {
      title: '远程工具',
      detail: remoteMcp.enabled ? '远程 MCP 已开放' : '支持远程 MCP 与 HTTP 工具，默认关闭',
      state: status(!!remoteMcp.enabled),
    },
    {
      title: '文档交付',
      detail: documents.length
        ? `${documents.map((item) => String(item).toUpperCase()).join(' · ')}${pptxReady ? ' · PPTX' : ' · PPTX 待配置'}`
        : '暂无可用格式',
      state: status(documents.length > 0),
    },
    {
      title: '分层记忆',
      detail: memory.supported ? `${(memory.scopes || []).length} 类作用域 · 自动摘要与人工维护` : '当前版本未启用',
      state: status(!!memory.supported),
    },
    {
      title: '专家协作',
      detail: experts.supported ? '并行成员 · 独立上下文 · 主管汇总' : '当前版本未启用',
      state: status(!!experts.supported),
    },
    {
      title: '自动化',
      detail: automation.supported ? '定时 · Cron · 单次 · Webhook' : '当前版本未启用',
      state: status(!!automation.supported),
    },
  ];
  $('capabilities').innerHTML = `
    <div class="capability-heading"><div><h2>能力状态</h2><p>平台默认边界与当前可用能力</p></div><span>${cards.filter((item) => item.state.className === 'ready').length}/${cards.length} 可使用</span></div>
    <div class="capability-grid">${cards.map((item) => `
      <article class="capability-card">
        <div><strong>${escapeHtml(item.title)}</strong><span class="capability-state ${item.state.className}">${escapeHtml(item.state.label)}</span></div>
        <p>${escapeHtml(item.detail)}</p>
      </article>`).join('')}
    </div>`;
}
function renderModels() { $('modelCount').textContent = state.models.length; $('modelList').innerHTML = state.models.map((m) => `<div class="card ${state.selectedModel?.id === m.id ? 'active' : ''}" data-model="${escapeHtml(m.id)}"><div class="card-title"><span>${escapeHtml(m.name)}</span><span class="status">${m.enabled ? '已启用' : '已停用'}</span></div><div class="card-desc">${escapeHtml(m.provider)} · ${escapeHtml(m.model)}</div><div class="small">${escapeHtml(m.id)} · ${m.has_api_key ? 'API Key 已在本机加密保存' : (m.api_key_env ? `环境变量 ${escapeHtml(m.api_key_env)}` : '无需密钥')}</div></div>`).join('') || '<div class="meta empty">尚未配置模型</div>'; $('agentModel').innerHTML = state.models.filter((m) => m.enabled).map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join(''); document.querySelectorAll('[data-model]').forEach((el) => el.onclick = () => selectModel(el.dataset.model)); }
function toggleModelKeyMode() { const direct = $('modelKeyMode').value === 'direct'; $('modelKeyEnvField').classList.toggle('hidden', direct); $('modelKeyDirectField').classList.toggle('hidden', !direct); $('modelKeyStatus').textContent = direct ? (state.selectedModel?.has_api_key ? '本机已保存加密密钥；输入新值可替换，留空保持不变。' : '密钥将在本机加密保存；生产部署建议接入专用密钥服务。') : '平台运行时从服务端环境变量读取密钥。'; }
function newModel() { state.selectedModel = null; renderModels(); $('deleteModelBtn').classList.add('hidden'); $('saveModelBtn').disabled = false; $('modelEditorTitle').textContent = '添加模型配置'; $('modelId').disabled = false; $('modelId').value = 'openai-main'; $('modelName').value = 'OpenAI 主模型'; $('modelProvider').value = 'openai_compatible'; $('modelNameValue').value = ''; $('modelBaseUrl').value = 'https://api.openai.com/v1'; $('modelKeyMode').value = 'env'; $('modelApiKeyEnv').value = 'OPENAI_API_KEY'; $('modelApiKey').value = ''; $('modelConfig').value = '{"temperature":0.2,"timeout":90}'; $('modelEnabled').checked = true; $('modelTestResult').textContent = '填写后保存，再测试连接'; toggleModelKeyMode(); }
function selectModel(id) { const m = state.models.find((x) => x.id === id); if (!m) return; state.selectedModel = m; renderModels(); $('deleteModelBtn').classList.toggle('hidden', id === 'deterministic'); $('modelId').disabled = true; $('modelId').value = m.id; $('modelName').value = m.name; $('modelProvider').value = m.provider; $('modelNameValue').value = m.model; $('modelBaseUrl').value = m.base_url || ''; $('modelKeyMode').value = m.api_key_mode || (m.has_api_key ? 'direct' : 'env'); $('modelApiKeyEnv').value = m.api_key_env || ''; $('modelApiKey').value = ''; $('modelConfig').value = formatJson(m.config || {}); $('modelEnabled').checked = !!m.enabled; if (id === 'deterministic') { $('modelEditorTitle').textContent = '内置离线模型（只读）'; $('modelKeyStatus').textContent = '内置模型不需要密钥。'; $('saveModelBtn').disabled = true; $('modelTestResult').textContent = '离线确定性模型可直接使用，无需连接测试'; return; } $('saveModelBtn').disabled = false; $('modelEditorTitle').textContent = m.name; toggleModelKeyMode(); }

async function deleteModel() {
  const model = state.selectedModel;
  if (!model || model.id === 'deterministic') return;
  if (!confirm(`确定删除模型“${model.name}”吗？`)) return;
  await api(`/api/models/${model.id}`, { method: 'DELETE' });
  state.models = await api('/api/models');
  newModel(); renderModels(); renderTaskModelSelect(); notify(`模型“${model.name}”已删除`);
}
async function saveModel() {
  const button = $('saveModelBtn'); setBusy(button, true);
  try {
    const payload = { id: $('modelId').value.trim(), name: $('modelName').value.trim(), provider: $('modelProvider').value, model: $('modelNameValue').value.trim(), base_url: $('modelBaseUrl').value.trim(), api_key_mode: $('modelKeyMode').value, api_key_env: $('modelApiKeyEnv').value.trim(), config: JSON.parse($('modelConfig').value || '{}'), enabled: $('modelEnabled').checked };
    if (payload.api_key_mode === 'direct' && $('modelApiKey').value) payload.api_key = $('modelApiKey').value;
    if (!payload.id || !payload.name || !payload.model) throw new Error('请填写 ID、名称和模型名');
    if (state.selectedModel?.id === 'deterministic') throw new Error('内置离线模型不能修改，请点击“添加模型”');
    if (state.selectedModel) await api(`/api/models/${state.selectedModel.id}`, { method: 'PUT', body: JSON.stringify(payload) });
    else await api('/api/models', { method: 'POST', body: JSON.stringify(payload) });
    state.models = await api('/api/models'); selectModel(payload.id); renderModels(); renderTaskModelSelect();
    $('modelTestResult').textContent = `已保存：${payload.name}（${payload.model}）\n现在可点击“测试连接”，也可在工作台选择该模型。`;
    notify(`模型“${payload.name}”已保存，可在智能体配置中选择`);
  } catch (err) {
    $('modelTestResult').textContent = `保存失败：${err.message || err}`;
    notify(`模型保存失败：${err.message || err}`, 'error');
  } finally { setBusy(button, false); }
}
async function testModel() {
  const modelId = state.selectedModel?.id || $('modelId').value.trim();
  if (!modelId || (!state.models.some((m) => m.id === modelId))) { notify('请先保存并选择一个模型', 'error'); return; }
  const button = $('testModelBtn'); setBusy(button, true, '测试中…'); $('modelTestResult').textContent = '正在连接模型…';
  try {
    const result = await api(`/api/models/${modelId}/test`, { method: 'POST' });
    $('modelTestResult').textContent = formatJson(result);
    notify('模型连接测试成功');
  } catch (err) {
    $('modelTestResult').textContent = String(err.message || err);
    notify(`模型连接失败：${err.message || err}`, 'error');
  } finally { setBusy(button, false); }
}

async function uploadFiles(files) { for (const file of files) { const form = new FormData(); form.append('file', file); state.uploads.push(await api('/api/uploads', { method: 'POST', body: form })); } renderUploads(); }
function renderUploads() { $('uploadList').innerHTML = state.uploads.map((f) => `<span class="upload-chip">${escapeHtml(f.name)} · ${Math.ceil(f.size / 1024)}KB</span>`).join(''); }
async function installSkillPackage(file) { try { const form = new FormData(); form.append('file', file); const installed = await api('/api/skills/install/upload', { method: 'POST', body: form }); state.skills = await api('/api/skills'); await selectSkill(installed.id); notify(`技能“${installed.name}”安装成功`); } catch (err) { notify(`技能安装失败：${err.message || err}`, 'error'); } }
async function importMcpPackage(file) { try { const form = new FormData(); form.append('file', file); const imported = await api('/api/mcp/import', { method: 'POST', body: form }); state.mcp = await api('/api/mcp'); renderMcp(); if (imported[0]) await selectMcp(imported[0].id); notify(`已导入 ${imported.length} 个工具服务`); } catch (err) { notify(`工具配置导入失败：${err.message || err}`, 'error'); } }
async function installSkillFromUrl() { const url = $('skillDownloadUrl').value.trim(); if (!url) return notify('请粘贴 Skill 下载直链', 'error'); try { const installed = await api('/api/skills/install/url', { method:'POST', body:JSON.stringify({url}) }); state.skills = await api('/api/skills'); await selectSkill(installed.id); notify(`技能“${installed.name}”安装成功`); } catch (err) { notify(`Skill 链接安装失败：${err.message || err}`, 'error'); } }
async function installMcpFromUrl() { const url = $('mcpDownloadUrl').value.trim(); if (!url) return notify('请粘贴 MCP JSON 下载直链', 'error'); try { const imported = await api('/api/mcp/install/url', { method:'POST', body:JSON.stringify({url}) }); state.mcp = await api('/api/mcp'); if (imported[0]) await selectMcp(imported[0].id); notify(`已安装 ${imported.length} 个工具服务`); } catch (err) { notify(`MCP 链接安装失败：${err.message || err}`, 'error'); } }

async function loadTasksOnly() {
  state.tasks = await api('/api/tasks');
  renderTasks();
}

async function loadLoopsOnly() {
  const selectedId = state.selectedLoop?.id;
  state.loops = await api('/api/loops');
  renderLoops();
  if (selectedId && state.loops.some((item) => item.id === selectedId)) await selectLoop(selectedId);
  else if (selectedId) newLoop();
}

function loopStatusLabel(status) {
  return ({
    paused: '已暂停', active: '等待触发', queued: '已排队', accepted: '已接收', running: '执行中',
    waiting_approval: '等待审批', blocked: '等待补充', completed: '已完成', failed: '失败', read: '已读', unread: '未读',
  })[status] || status;
}

function loopTriggerLabel(item) {
  const trigger = typeof item === 'string' ? item : item?.trigger_type;
  if (trigger === 'cron') return `Cron · ${item?.cron_expression || '未填写'}`;
  if (trigger === 'once') return `一次 · ${displayAutomationTime(item?.once_at)}`;
  if (trigger === 'webhook') return 'Webhook 事件';
  return `每 ${Number(item?.interval_seconds || 3600)} 秒`;
}

function displayAutomationTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

function toDateTimeLocal(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function automationDiffSummary(diff) {
  const changes = Array.isArray(diff?.changes) ? diff.changes : [];
  if (!diff?.changed || !changes.length) return '无变化';
  return `${changes.length}${diff.truncated ? '+' : ''} 项变化`;
}

function compactAutomationValue(value) {
  const raw = typeof value === 'string' ? value : JSON.stringify(value);
  if (raw == null) return 'null';
  return raw.length > 180 ? `${raw.slice(0, 177)}…` : raw;
}

function renderLoops() {
  const agent = $('loopAgent');
  const model = $('loopModel');
  const agentValue = agent?.value || state.selectedLoop?.agent_id || 'general-agent';
  const modelValue = model?.value || state.selectedLoop?.model_id || 'deterministic';
  if (agent) {
    agent.innerHTML = state.agents.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
    if (state.agents.some((item) => item.id === agentValue)) agent.value = agentValue;
  }
  if (model) {
    model.innerHTML = state.models.filter((item) => item.enabled).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
    if (state.models.some((item) => item.id === modelValue && item.enabled)) model.value = modelValue;
  }
  $('loopList').innerHTML = state.loops.map((item) => `
    <div class="card loop-card ${state.selectedLoop?.id === item.id ? 'active' : ''}" data-loop="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${escapeHtml(item.status)}">${escapeHtml(loopStatusLabel(item.status))}</span></div>
      <div class="card-desc">${escapeHtml(item.prompt)}</div>
      <div class="loop-card-stats"><span>${item.run_count}/${item.max_runs} 轮</span><span>${escapeHtml(loopTriggerLabel(item))}</span><span>${item.consecutive_failures}/${item.max_failures} 失败</span></div>
      <div class="small">${item.next_run_at ? `下次：${escapeHtml(displayAutomationTime(item.next_run_at))}` : item.trigger_type === 'webhook' && item.status === 'active' ? '正在等待 Webhook 事件' : '当前未安排下一次触发'}</div>
    </div>
  `).join('') || '<div class="meta empty">还没有自动化。创建一个持续目标开始使用。</div>';
  document.querySelectorAll('[data-loop]').forEach((el) => el.onclick = () => selectLoop(el.dataset.loop));
}

function renderLoopOverview(item = null) {
  $('loopOverviewStatus').textContent = item ? loopStatusLabel(item.status) : '尚未保存';
  $('loopOverviewTrigger').textContent = item ? loopTriggerLabel(item) : '未配置';
  $('loopOverviewNext').textContent = item?.next_run_at
    ? displayAutomationTime(item.next_run_at)
    : item?.trigger_type === 'webhook' && item?.status === 'active' ? '等待事件' : '—';
  $('loopOverviewDiff').textContent = item ? automationDiffSummary(item.last_diff) : '—';
}

function syncLoopTriggerFields() {
  const trigger = $('loopTriggerType').value;
  $('loopIntervalField').classList.toggle('hidden', trigger !== 'interval');
  $('loopCronField').classList.toggle('hidden', trigger !== 'cron');
  $('loopOnceField').classList.toggle('hidden', trigger !== 'once');
  $('loopWebhookFields').classList.toggle('hidden', trigger !== 'webhook');
  $('loopScheduleHint').textContent = ({
    interval: '按固定秒数重复触发', cron: '按 5 段 Cron 表达式触发', once: '在指定时间执行一次', webhook: '由签名 HTTP 请求触发',
  })[trigger];
  $('startLoopBtn').textContent = trigger === 'webhook' ? '启用 Webhook' : trigger === 'once' ? '启动一次性调度' : '启动调度';
  const id = state.selectedLoop?.id || $('loopId').value.trim() || '{automation_id}';
  const path = `/api/loops/${encodeURIComponent(id)}/webhook`;
  $('loopWebhookUrl').textContent = location.protocol === 'http:' || location.protocol === 'https:' ? `${location.origin}${path}` : path;
  const configured = Boolean(state.selectedLoop?.webhook_secret_configured);
  $('loopWebhookSecretStatus').textContent = configured
    ? '签名密钥已安全保存；留空不会覆盖，填写新值可轮换密钥。'
    : '尚未配置签名密钥；Webhook 触发必须填写至少 16 位密钥。';
}

function syncLoopActionButtons(item = state.selectedLoop) {
  if (!item) {
    ['runLoopBtn', 'startLoopBtn', 'pauseLoopBtn'].forEach((id) => { $(id).disabled = true; });
    return;
  }
  $('runLoopBtn').disabled = item.status === 'running' || item.run_count >= item.max_runs;
  $('startLoopBtn').disabled = ['running', 'active'].includes(item.status) || item.run_count >= item.max_runs || (item.trigger_type === 'once' && item.run_count > 0);
  $('pauseLoopBtn').disabled = !['running', 'active'].includes(item.status);
}

function newLoop() {
  clearTimeout(state.loopPollTimer);
  state.loopPollTimer = null;
  state.selectedLoop = null;
  state.loopTriggerEvents = [];
  state.loopNotifications = [];
  state.loopEditorDirty = false;
  state.loopStateDirty = false;
  renderLoops();
  $('loopEditorTitle').textContent = '新建自动化';
  $('loopStatusText').textContent = '配置触发条件后可试运行或启动调度';
  $('loopId').disabled = false; $('loopId').value = '';
  $('loopName').value = ''; $('loopPrompt').value = '';
  $('loopTriggerType').value = 'interval'; $('loopInterval').value = '3600'; $('loopCronExpression').value = ''; $('loopOnceAt').value = '';
  $('loopWebhookSecret').value = ''; $('loopWebhookTolerance').value = '300';
  $('loopMaxRuns').value = '10'; $('loopMaxFailures').value = '3'; $('loopMaxAttempts').value = '1'; $('loopRetryBackoff').value = '0';
  $('loopState').disabled = false; $('loopState').value = '{}'; $('loopStateTitle').textContent = '初始状态'; $('loopStateHint').textContent = '首轮运行可读取的结构化状态，必须填写 JSON 对象';
  $('deleteLoopBtn').classList.add('hidden');
  syncLoopActionButtons(null);
  renderLoopOverview();
  syncLoopTriggerFields();
  $('loopRunSummary').textContent = '';
  $('loopRuns').innerHTML = '<div class="meta empty">保存并选择自动化后查看历史</div>';
  $('loopTriggerEventSummary').textContent = '0 条';
  $('loopTriggerEvents').innerHTML = '<div class="meta empty">保存并选择自动化后查看触发记录</div>';
  $('loopNotificationSummary').textContent = '0 条';
  $('loopNotifications').innerHTML = '<div class="meta empty">暂无通知</div>';
}

async function selectLoop(id) {
  clearTimeout(state.loopPollTimer);
  state.loopPollTimer = null;
  const item = await api(`/api/loops/${id}`);
  const [triggerEvents, notifications] = await Promise.all([
    api(`/api/loops/${id}/trigger-events`).catch(() => []),
    api('/api/notifications?limit=200').catch(() => []),
  ]);
  state.selectedLoop = item;
  state.loopTriggerEvents = triggerEvents;
  state.loopNotifications = notifications.filter((notice) => notice.entity_type === 'loop' && notice.entity_id === id);
  renderLoops();
  $('loopEditorTitle').textContent = item.name;
  $('loopStatusText').textContent = `${loopStatusLabel(item.status)} · 已运行 ${item.run_count}/${item.max_runs} 轮${item.next_run_at ? ` · 下次 ${displayAutomationTime(item.next_run_at)}` : ''}`;
  $('loopId').disabled = true; $('loopId').value = item.id;
  $('loopName').value = item.name; $('loopPrompt').value = item.prompt;
  $('loopAgent').value = item.agent_id; $('loopModel').value = item.model_id;
  $('loopTriggerType').value = item.trigger_type || 'interval'; $('loopInterval').value = item.interval_seconds;
  $('loopCronExpression').value = item.cron_expression || ''; $('loopOnceAt').value = toDateTimeLocal(item.once_at);
  $('loopWebhookSecret').value = ''; $('loopWebhookTolerance').value = item.webhook_tolerance_seconds || 300;
  $('loopMaxRuns').value = item.max_runs; $('loopMaxFailures').value = item.max_failures;
  $('loopMaxAttempts').value = item.max_attempts || 1; $('loopRetryBackoff').value = item.retry_backoff_seconds || 0;
  $('loopState').value = formatJson(item.state || {}); $('loopState').disabled = item.status === 'running';
  $('loopStateTitle').textContent = '当前状态';
  $('loopStateHint').textContent = item.status === 'running' ? '本轮执行中，为避免覆盖运行结果，当前状态暂不可编辑' : '下一轮会读取此状态；保存后会替换当前结构化状态';
  $('deleteLoopBtn').classList.remove('hidden');
  syncLoopActionButtons(item);
  renderLoopOverview(item);
  syncLoopTriggerFields();
  renderLoopRuns(item.runs || []);
  renderLoopTriggerEvents();
  renderLoopNotifications();
  state.loopEditorDirty = false;
  state.loopStateDirty = false;
  scheduleLoopRefresh(id, item.status);
}

function scheduleLoopRefresh(id, status) {
  if (!document.querySelector('#tab-loops.active') || !['active', 'running'].includes(status)) return;
  state.loopPollTimer = setTimeout(() => {
    if (state.selectedLoop?.id !== id || !document.querySelector('#tab-loops.active')) return;
    const refresh = state.loopEditorDirty ? refreshSelectedLoopRuntime : selectLoop;
    refresh(id).catch((err) => notify(`自动化状态刷新失败：${err.message || err}`, 'error'));
  }, status === 'running' ? 1200 : 5000);
}

async function refreshSelectedLoopRuntime(id) {
  const [item, triggerEvents, notifications] = await Promise.all([
    api(`/api/loops/${id}`),
    api(`/api/loops/${id}/trigger-events`).catch(() => []),
    api('/api/notifications?limit=200').catch(() => []),
  ]);
  if (state.selectedLoop?.id !== id) return;
  state.selectedLoop = item;
  state.loopTriggerEvents = triggerEvents;
  state.loopNotifications = notifications.filter((notice) => notice.entity_type === 'loop' && notice.entity_id === id);
  const { runs: _runs, ...loopSnapshot } = item;
  state.loops = state.loops.map((loop) => loop.id === id ? { ...loop, ...loopSnapshot } : loop);
  renderLoops();
  $('loopStatusText').textContent = `${loopStatusLabel(item.status)} · 已运行 ${item.run_count}/${item.max_runs} 轮${item.next_run_at ? ` · 下次 ${displayAutomationTime(item.next_run_at)}` : ''}`;
  $('loopState').disabled = item.status === 'running';
  $('loopStateHint').textContent = item.status === 'running' ? '本轮执行中，为避免覆盖运行结果，当前状态暂不可编辑' : '表单有尚未保存的修改；运行状态已刷新，但不会覆盖你的输入';
  syncLoopActionButtons(item);
  renderLoopOverview(item);
  renderLoopRuns(item.runs || []);
  renderLoopTriggerEvents();
  renderLoopNotifications();
  scheduleLoopRefresh(id, item.status);
}

function renderStateDiff(diff) {
  const changes = Array.isArray(diff?.changes) ? diff.changes : [];
  if (!diff?.changed || !changes.length) return '<div class="automation-no-change">本次运行未改变结构化状态</div>';
  return `<div class="automation-diff-list">${changes.map((change) => `
    <div class="automation-diff-row">
      <code>${escapeHtml(change.path || '$')}</code>
      <span title="${escapeHtml(compactAutomationValue(change.before))}">${escapeHtml(compactAutomationValue(change.before))}</span>
      <i aria-hidden="true">→</i>
      <strong title="${escapeHtml(compactAutomationValue(change.after))}">${escapeHtml(compactAutomationValue(change.after))}</strong>
    </div>
  `).join('')}</div>${diff.truncated ? '<div class="small">变化项较多，仅展示后端返回的前 200 项。</div>' : ''}`;
}

function renderLoopRuns(runs) {
  const uniqueRounds = new Set(runs.map((run) => run.run_number)).size;
  $('loopRunSummary').textContent = `${uniqueRounds} 轮 · ${runs.length} 次尝试`;
  $('loopRuns').innerHTML = runs.map((run, index) => `
    <details class="loop-run-detail ${escapeHtml(run.status)}" ${index === 0 ? 'open' : ''}>
      <summary>
        <span class="loop-run-number">${run.run_number}</span>
        <span class="loop-run-copy"><strong>第 ${run.run_number} 轮 · 尝试 ${run.attempt} · ${escapeHtml(loopStatusLabel(run.status))}</strong><small>${escapeHtml(run.decision?.reason || run.error?.message || '运行记录')} · ${escapeHtml(displayAutomationTime(run.finished_at || run.started_at))}</small></span>
        <span class="automation-diff-badge ${run.diff?.changed ? 'changed' : ''}">${escapeHtml(automationDiffSummary(run.diff))}</span>
      </summary>
      <div class="loop-run-body">
        <div class="loop-run-meta"><span>开始：${escapeHtml(displayAutomationTime(run.started_at))}</span><span>结束：${escapeHtml(displayAutomationTime(run.finished_at))}</span>${run.trigger_event_id ? `<span>触发事件：${escapeHtml(run.trigger_event_id)}</span>` : ''}</div>
        <div class="automation-state-diff"><h4>本次状态差异</h4>${renderStateDiff(run.diff)}</div>
        ${run.error?.message ? `<div class="automation-run-error">${escapeHtml(run.error.message)}</div>` : ''}
        ${run.task_id ? `<button class="text-button loop-open-task" data-loop-task="${escapeHtml(run.task_id)}">查看本轮任务与产物 →</button>` : ''}
      </div>
    </details>
  `).join('') || '<div class="meta empty">尚未执行。可立即运行一轮验证配置。</div>';
  document.querySelectorAll('[data-loop-task]').forEach((el) => {
    el.onclick = (event) => { event.stopPropagation(); if (el.dataset.loopTask) openTask(el.dataset.loopTask); };
  });
}

function renderLoopTriggerEvents() {
  const events = state.loopTriggerEvents || [];
  $('loopTriggerEventSummary').textContent = `${events.length} 条`;
  $('loopTriggerEvents').innerHTML = events.map((event) => `
    <article class="automation-event-item">
      <div><span class="status ${escapeHtml(event.status)}">${escapeHtml(loopStatusLabel(event.status))}</span><strong>${escapeHtml(event.trigger_type === 'webhook' ? 'Webhook' : event.trigger_type || '手动')} 触发</strong><time>${escapeHtml(displayAutomationTime(event.received_at))}</time></div>
      <div class="automation-event-meta"><span>幂等键：<code>${escapeHtml(event.idempotency_key || '—')}</code></span><span>请求摘要：<code>${escapeHtml((event.payload_sha256 || '').slice(0, 16) || '—')}</code></span></div>
      ${event.error ? `<p>${escapeHtml(event.error)}</p>` : ''}
    </article>
  `).join('') || '<div class="meta empty">尚无触发事件。手动试运行、计划调度或 Webhook 到达后会显示在这里。</div>';
}

function renderLoopNotifications() {
  const filter = $('loopNotificationFilter').value || 'all';
  const all = state.loopNotifications || [];
  const notices = filter === 'all' ? all : all.filter((notice) => notice.status === filter);
  const unread = all.filter((notice) => notice.status === 'unread').length;
  $('loopNotificationSummary').textContent = `${all.length} 条 · ${unread} 未读`;
  $('loopNotifications').innerHTML = notices.map((notice) => `
    <article class="automation-notice ${escapeHtml(notice.status)}">
      <div><span class="automation-unread-dot" aria-hidden="true"></span><strong>${escapeHtml(notice.title)}</strong><time>${escapeHtml(displayAutomationTime(notice.created_at))}</time></div>
      <p>${escapeHtml(notice.content)}</p>
      ${notice.status === 'unread' ? `<button class="text-button" data-notification-read="${escapeHtml(notice.id)}">标记已读</button>` : `<span class="small">已于 ${escapeHtml(displayAutomationTime(notice.read_at))} 阅读</span>`}
    </article>
  `).join('') || `<div class="meta empty">${all.length ? '当前筛选条件下没有通知' : '自动化完成、失败、等待审批或需要补充信息时会在这里通知。'}</div>`;
  document.querySelectorAll('[data-notification-read]').forEach((button) => {
    button.onclick = () => markLoopNotificationRead(button.dataset.notificationRead, button);
  });
}

async function markLoopNotificationRead(notificationId, button) {
  setBusy(button, true, '处理中…');
  try {
    const updated = await api(`/api/notifications/${notificationId}/read`, { method: 'POST' });
    state.loopNotifications = state.loopNotifications.map((notice) => notice.id === updated.id ? updated : notice);
    renderLoopNotifications();
  } catch (err) {
    setBusy(button, false);
    notify(`通知更新失败：${err.message || err}`, 'error');
  }
}

function loopPayload() {
  let automationState;
  try { automationState = JSON.parse($('loopState').value.trim() || '{}'); }
  catch (_) { throw new Error('状态必须是有效的 JSON 对象'); }
  if (!automationState || Array.isArray(automationState) || typeof automationState !== 'object') throw new Error('状态必须是 JSON 对象，不能是数组或普通文本');
  const triggerType = $('loopTriggerType').value;
  const intervalSeconds = Number($('loopInterval').value);
  const maxRuns = Number($('loopMaxRuns').value);
  const maxFailures = Number($('loopMaxFailures').value);
  const maxAttempts = Number($('loopMaxAttempts').value);
  const retryBackoff = Number($('loopRetryBackoff').value);
  if (triggerType === 'interval' && (!Number.isInteger(intervalSeconds) || intervalSeconds < 5)) throw new Error('固定间隔不能少于 5 秒');
  if (![maxRuns, maxFailures, maxAttempts, retryBackoff].every(Number.isInteger)) throw new Error('运行次数和重试设置必须填写整数');
  if (maxRuns < 1) throw new Error('最大轮数至少为 1');
  if (maxFailures < 1) throw new Error('连续失败熔断阈值至少为 1');
  if (maxAttempts < 1 || maxAttempts > 10) throw new Error('每轮最多尝试次数必须在 1 到 10 之间');
  if (retryBackoff < 0 || retryBackoff > 3600) throw new Error('重试等待必须在 0 到 3600 秒之间');
  const cronExpression = $('loopCronExpression').value.trim();
  if (triggerType === 'cron' && !cronExpression) throw new Error('请填写 Cron 表达式');
  let onceAt = '';
  if (triggerType === 'once') {
    const value = $('loopOnceAt').value;
    const parsed = new Date(value);
    if (!value || Number.isNaN(parsed.getTime())) throw new Error('请选择一次性执行时间');
    if (parsed.getTime() <= Date.now()) throw new Error('一次性执行时间必须晚于当前时间');
    onceAt = parsed.toISOString();
  }
  const payload = {
    name: $('loopName').value.trim(), prompt: $('loopPrompt').value.trim(),
    agent_id: $('loopAgent').value, model_id: $('loopModel').value,
    trigger_type: triggerType, interval_seconds: intervalSeconds || 3600,
    cron_expression: cronExpression, once_at: onceAt,
    webhook_tolerance_seconds: Number($('loopWebhookTolerance').value) || 300,
    max_runs: maxRuns, max_failures: maxFailures,
    max_attempts: maxAttempts, retry_backoff_seconds: retryBackoff,
  };
  if (!state.selectedLoop) {
    payload.id = $('loopId').value.trim() || undefined;
    payload.initial_state = automationState;
  } else if (state.loopStateDirty && state.selectedLoop.status !== 'running') payload.state = automationState;
  const webhookSecret = $('loopWebhookSecret').value;
  if (triggerType === 'webhook') {
    const tolerance = Number($('loopWebhookTolerance').value);
    if (!Number.isInteger(tolerance) || tolerance < 30 || tolerance > 3600) throw new Error('Webhook 时间戳容差必须在 30 到 3600 秒之间');
    if (webhookSecret && webhookSecret.length < 16) throw new Error('Webhook 签名密钥至少需要 16 位');
    if (!webhookSecret && !state.selectedLoop?.webhook_secret_configured) throw new Error('Webhook 触发必须配置至少 16 位签名密钥');
    if (webhookSecret) payload.webhook_secret = webhookSecret;
  }
  return payload;
}

async function saveLoop() {
  const button = $('saveLoopBtn'); setBusy(button, true);
  try {
    const payload = loopPayload();
    if (!payload.name || !payload.prompt) throw new Error('请填写名称和自动化目标');
    const saved = state.selectedLoop
      ? await api(`/api/loops/${state.selectedLoop.id}`, { method: 'PUT', body: JSON.stringify(payload) })
      : await api('/api/loops', { method: 'POST', body: JSON.stringify(payload) });
    state.loops = await api('/api/loops'); await selectLoop(saved.id); notify(`自动化“${saved.name}”已保存`);
  } catch (err) { notify(`自动化保存失败：${err.message || err}`, 'error'); }
  finally {
    setBusy(button, false);
    syncLoopTriggerFields();
    syncLoopActionButtons();
  }
}

async function loopAction(action) {
  if (!state.selectedLoop) return;
  const button = $({ run: 'runLoopBtn', start: 'startLoopBtn', pause: 'pauseLoopBtn' }[action]);
  setBusy(button, true, action === 'run' ? '提交中…' : '处理中…');
  try {
    await api(`/api/loops/${state.selectedLoop.id}/${action}`, { method: 'POST' });
    if (action === 'run') await new Promise((resolve) => setTimeout(resolve, 450));
    state.loops = await api('/api/loops'); await selectLoop(state.selectedLoop.id);
    notify(action === 'run' ? '试运行已提交，运行历史会自动刷新' : action === 'start' ? '自动化调度已启动' : '自动化已暂停');
  } catch (err) { notify(`操作失败：${err.message || err}`, 'error'); }
  finally {
    setBusy(button, false);
    syncLoopTriggerFields();
    syncLoopActionButtons();
  }
}

async function deleteLoop() {
  if (!state.selectedLoop || !confirm(`确定删除自动化“${state.selectedLoop.name}”及其运行索引吗？已生成的普通任务和文件会保留。`)) return;
  await api(`/api/loops/${state.selectedLoop.id}`, { method: 'DELETE' });
  state.loops = await api('/api/loops'); newLoop(); notify('自动化已删除，历史普通任务和产物仍保留');
}

function renderTasks() {
  $('taskList').innerHTML = state.tasks.map((t) => `
    <div class="task-card" data-task="${escapeHtml(t.id)}">
      <div class="card-title"><span>${escapeHtml(t.title)}</span><span class="status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></div>
      <div class="card-desc">${escapeHtml(t.message)}</div>
      <div class="small">${escapeHtml(t.id)} · ${escapeHtml(t.created_at)}</div>
    </div>
  `).join('');
  document.querySelectorAll('[data-task]').forEach((el) => el.onclick = () => openTask(el.dataset.task));
}

async function openTask(id) {
  stopTaskStream();
  const task = await api(`/api/tasks/${id}`);
  state.currentTask = task;
  state.currentExpertSelection = null;
  setWorkbenchMode(task.executor_type === 'team' ? 'expert' : 'agent', { persist: false });
  if (task.executor_type === 'team' && $('expertTeamSelect') && enabledWorkbenchTeams().some((team) => team.id === task.executor_id)) {
    $('expertTeamSelect').value = task.executor_id;
    renderWorkbenchMode();
  }
  if (task.conversation_id) {
    state.conversationId = task.conversation_id;
    writePreference('conversation', state.conversationId);
  }
  switchTab('chat');
  renderTaskMeta(task);
  await watchTaskRuntime(id);
  await renderConversation(state.conversationId);
  $('timeline').innerHTML = '';
  (task.events || []).forEach(appendEvent);
  renderArtifacts(task.artifacts || []);
  if (runtimeIsActive(currentRuntimeStatus())) {
    startTaskStream(id, { seenEventIds: (task.events || []).map((event) => event.id) });
  }
}

async function renderConversation(conversationId) {
  const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
  $('conversation').innerHTML = '';
  for (const message of data.messages || []) {
    addMessage(message.role === 'assistant' ? 'agent' : 'user', message.content, message.event_id || null);
  }
  return (data.messages || []).length > 0;
}

function newConversation() {
  stopTaskStream();
  state.currentTask = null;
  state.currentExpertSelection = null;
  state.conversationId = createConversationId();
  writePreference('conversation', state.conversationId);
  $('conversation').innerHTML = '';
  $('timeline').innerHTML = '';
  $('taskMeta').className = 'meta empty';
  $('taskMeta').textContent = '尚未创建任务';
  resetTaskRuntime();
  renderArtifacts([]);
  addMessage('agent', '新对话已开始。你可以继续描述要完成的事情。');
  loadMemoriesOnly({ preserveSelection: false }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
}

function setSidebarCollapsed(collapsed) {
  const sidebar = $('sidebar');
  const toggle = $('sidebarToggle');
  if (!sidebar || !toggle) return;
  sidebar.classList.toggle('collapsed', collapsed);
  toggle.setAttribute('aria-label', collapsed ? '展开侧边栏' : '收起侧边栏');
  toggle.title = collapsed ? '展开侧边栏' : '收起侧边栏';
  writePreference('sidebar-collapsed', collapsed ? '1' : '0');
}

function initSidebar() {
  const collapsed = readPreference('sidebar-collapsed') === '1';
  setSidebarCollapsed(collapsed);
}

function bindEvents() {
  document.querySelectorAll('.nav').forEach((btn) => btn.onclick = () => switchTab(btn.dataset.tab));
  $('sidebarToggle').onclick = () => setSidebarCollapsed(!$('sidebar').classList.contains('collapsed'));
  document.querySelectorAll('[data-workbench-mode]').forEach((button) => {
    button.onclick = () => setWorkbenchMode(button.dataset.workbenchMode);
  });
  $('expertTeamSelect').onchange = (event) => {
    writePreference('expert-team', event.target.value);
    renderWorkbenchMode();
  };
  $('sendBtn').onclick = sendTask;
  $('newConversationBtn').onclick = newConversation;
  $('agentSelect').onchange = (e) => {
    writePreference('agent', e.target.value);
    syncMemoryScopeId();
    loadMemoriesOnly({ preserveSelection: false }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  };
  $('taskModelSelect').onchange = (e) => writePreference('model', e.target.value);
  $('exampleBtn').onclick = () => {
    $('messageInput').value = state.workbenchMode === 'expert'
      ? '请从业务价值、交付风险和用户体验三个角度评审这份方案，归纳一致结论与主要分歧，并给出按优先级排序的改进建议。'
      : '帮我把本周工作进展整理成摘要、主要风险和下周行动计划。';
    $('messageInput').focus();
  };
  $('refreshBtn').onclick = loadAll;
  $('newSkillBtn').onclick = newSkill;
  $('skillPackageInput').onchange = (e) => e.target.files[0] && installSkillPackage(e.target.files[0]);
  $('installSkillUrlBtn').onclick = installSkillFromUrl;
  $('saveSkillBtn').onclick = saveSkill;
  $('deleteSkillBtn').onclick = () => deleteSkill().catch((err) => notify(`技能卸载失败：${err.message || err}`, 'error'));
  $('newSkillFileBtn').onclick = newSkillFile;
  $('exportSkillBtn').onclick = exportSelectedSkill;
  $('skillFileUploadInput').onchange = (e) => {
    const file = e.target.files?.[0];
    if (file) uploadSkillFile(file).catch((err) => notify(`文件上传失败：${err.message || err}`, 'error'));
    e.target.value = '';
  };
  $('saveSkillFileBtn').onclick = () => saveSkillFile().catch((err) => notify(`文件保存失败：${err.message || err}`, 'error'));
  $('deleteSkillFileBtn').onclick = () => deleteSkillFile().catch((err) => notify(`文件删除失败：${err.message || err}`, 'error'));
  $('invokeToolBtn').onclick = invokeTool;
  $('newMcpBtn').onclick = newMcp; $('saveMcpBtn').onclick = saveMcp; $('discoverMcpBtn').onclick = discoverMcp; $('deleteMcpBtn').onclick = () => deleteMcp().catch((err) => notify(`工具服务卸载失败：${err.message || err}`, 'error'));
  $('mcpImportInput').onchange = (e) => e.target.files[0] && importMcpPackage(e.target.files[0]);
  $('installMcpUrlBtn').onclick = installMcpFromUrl;
  $('newAgentBtn').onclick = newAgent; $('saveAgentBtn').onclick = saveAgent;
  $('reloadExpertsBtn').onclick = () => loadExpertWorkspace({ preserveSelection: true }).catch((err) => notify(`专家团刷新失败：${err.message || err}`, 'error'));
  $('newExpertTemplateBtn').onclick = newExpertTemplate;
  $('saveExpertTemplateBtn').onclick = saveExpertTemplate;
  $('deleteExpertTemplateBtn').onclick = deleteExpertTemplate;
  $('installExpertTemplateBtn').onclick = installSelectedExpertTemplate;
  $('newExpertTeamBtn').onclick = newExpertTeam;
  $('saveExpertTeamBtn').onclick = saveExpertTeam;
  $('deleteExpertTeamBtn').onclick = deleteExpertTeam;
  $('addExpertMemberBtn').onclick = addExpertTeamMember;
  $('runExpertTeamBtn').onclick = runSelectedExpertTeam;
  $('refreshMemoriesBtn').onclick = () => loadMemoriesOnly({ preserveSelection: true }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  $('newMemoryBtn').onclick = newMemory;
  $('saveMemoryBtn').onclick = saveMemory;
  $('deleteMemoryBtn').onclick = () => deleteMemory().catch((err) => notify(`记忆删除失败：${err.message || err}`, 'error'));
  $('previewMemoryContextBtn').onclick = () => loadEffectiveMemoryContext().catch((err) => notify(`上下文读取失败：${err.message || err}`, 'error'));
  $('conversationSummarySelect').onchange = (event) => selectConversationSummary(event.target.value);
  $('saveConversationSummaryBtn').onclick = saveConversationSummary;
  $('deleteConversationSummaryBtn').onclick = deleteConversationSummary;
  $('memoryScopeType').onchange = syncMemoryScopeId;
  $('memoryScopeFilter').onchange = renderMemories;
  $('memoryStatusFilter').onchange = renderMemories;
  $('reloadArtifactsBtn').onclick = () => loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物刷新失败：${err.message || err}`, 'error'));
  $('artifactKindFilter').onchange = () => loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物筛选失败：${err.message || err}`, 'error'));
  $('newModelBtn').onclick = newModel; $('saveModelBtn').onclick = saveModel; $('testModelBtn').onclick = testModel; $('deleteModelBtn').onclick = () => deleteModel().catch((err) => notify(`模型删除失败：${err.message || err}`, 'error'));
  $('modelKeyMode').onchange = toggleModelKeyMode;
  $('fileInput').onchange = (e) => uploadFiles(Array.from(e.target.files || []));
  $('reloadTasksBtn').onclick = loadTasksOnly;
  $('cancelTaskBtn').onclick = () => sendTaskRuntimeCommand('cancel', {}, $('cancelTaskBtn'));
  $('retryTaskBtn').onclick = () => sendTaskRuntimeCommand('retry', {}, $('retryTaskBtn'));
  $('resumeTaskBtn').onclick = () => sendTaskRuntimeCommand('resume', {}, $('resumeTaskBtn'));
  $('runtimeMessageBtn').onclick = submitRuntimeMessage;
  $('runtimeMessage').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitRuntimeMessage();
  });
  $('reloadLoopsBtn').onclick = loadLoopsOnly; $('newLoopBtn').onclick = newLoop; $('saveLoopBtn').onclick = saveLoop;
  $('runLoopBtn').onclick = () => loopAction('run'); $('startLoopBtn').onclick = () => loopAction('start'); $('pauseLoopBtn').onclick = () => loopAction('pause');
  $('deleteLoopBtn').onclick = () => deleteLoop().catch((err) => notify(`删除失败：${err.message || err}`, 'error'));
  $('loopTriggerType').onchange = () => { state.loopEditorDirty = true; syncLoopTriggerFields(); };
  $('loopId').oninput = () => { state.loopEditorDirty = true; syncLoopTriggerFields(); };
  [
    'loopName', 'loopAgent', 'loopModel', 'loopInterval', 'loopCronExpression', 'loopOnceAt',
    'loopWebhookSecret', 'loopWebhookTolerance', 'loopMaxRuns', 'loopMaxFailures',
    'loopMaxAttempts', 'loopRetryBackoff', 'loopPrompt',
  ].forEach((id) => $(id).addEventListener('input', () => { state.loopEditorDirty = true; }));
  $('loopState').addEventListener('input', () => { state.loopEditorDirty = true; state.loopStateDirty = true; });
  $('loopNotificationFilter').onchange = renderLoopNotifications;
  syncLoopTriggerFields();
  $('messageInput').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') sendTask();
  });
}

(async function init() {
  initSidebar();
  bindEvents();
  renderWorkbenchMode();
  window.addEventListener('unhandledrejection', (event) => notify(event.reason?.message || '操作失败，请检查平台连接', 'error'));
  if (location.protocol === 'file:') $('connectionBanner').classList.remove('hidden');
  try {
    await loadAll();
    if ($('serviceStatus')) {
      $('serviceStatus').classList.remove('offline');
      $('serviceStatus').querySelector('b').textContent = '服务已连接';
    }
    writePreference('conversation', state.conversationId);
    const restored = await renderConversation(state.conversationId);
    if (!restored) addMessage('agent', '你好，我可以帮你分析资料、调用工具并生成文档。直接告诉我你想完成什么。');
  } catch (err) {
    if ($('serviceStatus')) {
      $('serviceStatus').classList.add('offline');
      $('serviceStatus').querySelector('b').textContent = '服务未连接';
    }
    addMessage('agent', location.protocol === 'file:' ? '页面尚未连接到平台服务，请从已启动的平台地址访问。' : `加载平台配置失败：${err.message || err}`);
  }
})();
