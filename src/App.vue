<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

const state = reactive({
  config: { room_id: "", auto_connect: true, rules: [], device_generations: {} },
  waveforms: [],
  devices: [],
  listener: { connected: false, connecting: false, error: "", room_id: "" },
  events: [],
});
const page = ref("dashboard");
const busy = ref("");
const toast = ref("");
const activeRule = ref("");
const chartStreams = new Map();
const chartCanvases = new Map();
const configFileInput = ref(null);
let socket;
let reconnectTimer;
let chartFrame;
const chartPointCount = 120;
const chartSampleMs = 100;
const chartWindowMs = (chartPointCount - 1) * chartSampleMs;

const tierNames = {
  normal: "普通用户",
  captain: "舰长",
  admiral: "提督",
  governor: "总督",
};
const eventNames = {
  danmu: "指定弹幕",
  like: "点赞",
  gift: "礼物",
  guard_captain: "上舰长",
  guard_admiral: "上提督",
  guard_governor: "上总督",
  enter: "进入直播间",
  leave: "离开直播间",
  follow: "关注直播间",
  unfollow: "取关直播间",
  share: "分享直播间",
};
const modeNames = { loop: "循环", sequence: "顺序", random: "随机" };
const unavailableEvents = new Set(["leave", "unfollow"]);

const groupedRules = computed(() => {
  const groups = {};
  for (const rule of state.config.rules) {
    const key = rule.event_type;
    (groups[key] ||= []).push(rule);
  }
  return groups;
});

const connectedCount = computed(() => state.devices.filter((item) => item.connected).length);

function normalizeRuleModes(rules = state.config.rules) {
  for (const rule of rules || []) {
    if (!Array.isArray(rule.waveforms) || !rule.waveforms.length) continue;
    if (rule.waveforms.length === 1) rule.play_mode = "loop";
    else if (rule.play_mode === "loop") rule.play_mode = "sequence";
  }
}

function notify(message) {
  toast.value = message;
  window.setTimeout(() => {
    if (toast.value === message) toast.value = "";
  }, 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error((await response.text()) || `请求失败 ${response.status}`);
  return response.json();
}

function applySnapshot(data) {
  Object.assign(state, data);
  normalizeRuleModes();
  ensureOutputs();
  for (const scheduled of data.scheduler || []) {
    const device = state.devices.find((item) => item.id === scheduled.device_id);
    if (device) device.outputs[scheduled.channel] = scheduled.output;
  }
  seedChartStreams();
}

function ensureOutputs() {
  for (const device of state.devices) {
    device.outputs ||= {};
    for (const channel of ["A", "B"]) {
      device.outputs[channel] ||= {
        strength: 0, remaining: 0, total_remaining: 0, waveform: "", frequency: 1,
        pulse_width: 0, event_name: "", queue_size: 0, history: [],
      };
    }
  }
}

function chartKey(deviceId, channel) {
  return `${deviceId}::${channel}`;
}

function normalizeHistory(history) {
  const values = Array.isArray(history) ? history.slice(-chartPointCount) : [];
  return Array(Math.max(0, chartPointCount - values.length)).fill(0).concat(values);
}

function createChartStream(history) {
  const values = normalizeHistory(history);
  const now = performance.now();
  return {
    values,
    pending: [],
    lastAdvance: now,
    activeUntil: values.some((value) => value !== 0) ? now + chartWindowMs : now,
  };
}

function seedChartStreams() {
  for (const device of state.devices) {
    for (const channel of ["A", "B"]) {
      const key = chartKey(device.id, channel);
      if (!chartStreams.has(key)) {
        const history = device.outputs?.[channel]?.history;
        chartStreams.set(key, createChartStream(history));
      }
    }
  }
  requestChartRender();
}

function renderChartStream(stream, now) {
  while (now - stream.lastAdvance >= chartSampleMs) {
    const next = stream.pending.length
      ? stream.pending.shift()
      : stream.values[stream.values.length - 1] || 0;
    stream.values.shift();
    stream.values.push(next);
    stream.lastAdvance += chartSampleMs;
  }
  const progress = Math.max(
    0,
    Math.min(1, (now - stream.lastAdvance) / chartSampleMs),
  );
  const next = stream.pending[0] ?? stream.values[stream.values.length - 1] ?? 0;
  return { values: stream.values, next, progress };
}

function animateCharts(now) {
  let active = false;
  for (const deviceId of chartCanvases.keys()) {
    drawDeviceChart(deviceId, now);
  }
  for (const [key, stream] of chartStreams) {
    if (now <= stream.activeUntil) active = true;
  }
  chartFrame = active ? requestAnimationFrame(animateCharts) : undefined;
}

function requestChartRender() {
  if (!chartFrame) chartFrame = requestAnimationFrame(animateCharts);
}

function setChartCanvas(element) {
  if (element) {
    const deviceId = element.dataset.deviceId;
    chartCanvases.set(deviceId, element);
    requestChartRender();
  }
}

function drawGrid(context, width, height) {
  context.strokeStyle = "rgba(185,155,255,.08)";
  context.lineWidth = 1;
  context.beginPath();
  for (let x = 0; x <= width; x += width / 18) {
    context.moveTo(x, 0);
    context.lineTo(x, height);
  }
  for (let y = 0; y <= height; y += height / 4) {
    context.moveTo(0, y);
    context.lineTo(width, y);
  }
  context.stroke();
}

function drawWaveLine(context, frame, width, height, color) {
  const { values, next, progress } = frame;
  const stepX = width / Math.max(1, chartPointCount - 1);
  const points = [...values, next].map((rawValue, index) => {
    const value = Math.max(0, Math.min(100, rawValue || 0));
    return {
      x: (index - progress) * stepX,
      y: height - value / 100 * height,
    };
  });
  context.save();
  context.beginPath();
  context.rect(0, 0, width, height);
  context.clip();
  context.strokeStyle = color;
  context.lineWidth = 1.35;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.shadowColor = color;
  context.shadowBlur = 3;
  context.beginPath();
  context.moveTo(points[0].x, points[0].y);
  for (let index = 1; index < points.length - 1; index++) {
    const point = points[index];
    const following = points[index + 1];
    context.quadraticCurveTo(
      point.x,
      point.y,
      (point.x + following.x) / 2,
      (point.y + following.y) / 2,
    );
  }
  const final = points[points.length - 1];
  context.lineTo(final.x, final.y);
  context.stroke();
  context.restore();
}

function drawDeviceChart(deviceId, now) {
  const canvas = chartCanvases.get(deviceId);
  if (!canvas) return;
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(bounds.width * ratio);
  const pixelHeight = Math.round(bounds.height * ratio);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, bounds.width, bounds.height);
  drawGrid(context, bounds.width, bounds.height);
  const streamA = chartStreams.get(chartKey(deviceId, "A"));
  const streamB = chartStreams.get(chartKey(deviceId, "B"));
  const flat = {
    values: Array(chartPointCount).fill(0),
    next: 0,
    progress: 0,
  };
  drawWaveLine(
    context,
    streamA ? renderChartStream(streamA, now) : flat,
    bounds.width,
    bounds.height,
    "#b98aff",
  );
  drawWaveLine(
    context,
    streamB ? renderChartStream(streamB, now) : flat,
    bounds.width,
    bounds.height,
    "#52d9dc",
  );
}

function updateChartTarget(deviceId, channel, history) {
  const key = chartKey(deviceId, channel);
  const target = normalizeHistory(history);
  const now = performance.now();
  let stream = chartStreams.get(key);
  if (!stream) {
    stream = createChartStream(target);
    chartStreams.set(key, stream);
  } else {
    const next = target[target.length - 1];
    stream.pending.push(next);
    if (stream.pending.length > 4) {
      stream.pending.splice(0, stream.pending.length - 4);
    }
  }
  stream.activeUntil = now + chartWindowMs;
  requestChartRender();
}

function connectSocket() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "snapshot") applySnapshot(message.data);
    if (message.type === "devices") {
      state.devices = message.data;
      ensureOutputs();
      seedChartStreams();
    }
    if (message.type === "listener") state.listener = message.data;
    if (message.type === "config") {
      state.config = message.data;
      normalizeRuleModes();
    }
    if (message.type === "waveforms") state.waveforms = message.data;
    if (message.type === "live_event") {
      state.events.unshift(message.data);
      state.events = state.events.slice(0, 200);
    }
    if (message.type === "events") state.events = message.data;
    if (message.type === "channel_output") {
      const device = state.devices.find((item) => item.id === message.data.device_id);
      if (device) {
        updateChartTarget(
          message.data.device_id,
          message.data.channel,
          message.data.output.history,
        );
        device.outputs[message.data.channel] = message.data.output;
      }
    }
  };
  socket.onclose = () => {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectSocket, 1500);
  };
}

async function withBusy(key, action) {
  busy.value = key;
  try {
    await action();
  } catch (error) {
    notify(error.message);
  } finally {
    busy.value = "";
  }
}

function scan() {
  withBusy("scan", async () => {
    state.devices = await api("/api/devices/scan", { method: "POST" });
    ensureOutputs();
    notify(`发现 ${state.devices.length} 台设备`);
  });
}

function deviceAction(device, action) {
  withBusy(`${action}:${device.id}`, async () => {
    await api(`/api/devices/${encodeURIComponent(device.id)}/${action}`, { method: "POST" });
    notify(action === "connect" ? "设备已连接" : "设备已断开");
  });
}

async function setGeneration(device) {
  await api(`/api/devices/${encodeURIComponent(device.id)}/generation`, {
    method: "PUT",
    body: JSON.stringify({ generation: Number(device.generation) }),
  });
}

function listenerAction(action) {
  withBusy(`listener:${action}`, async () => {
    await api(`/api/listener/${action}`, {
      method: "POST",
      body: JSON.stringify({ room_id: state.config.room_id }),
    });
  });
}

function saveConfig() {
  withBusy("save", async () => {
    await api("/api/config", { method: "PUT", body: JSON.stringify(state.config) });
    notify("配置已保存");
  });
}

async function exportConfig() {
  await withBusy("export-config", async () => {
    const response = await fetch("/api/config/export");
    if (!response.ok) throw new Error(await response.text());
    const content = await response.text();
    const filename = `ycy-event-config-${new Date().toISOString().slice(0, 10)}.json`;
    if (window.pywebview?.api?.save_config) {
      const result = await window.pywebview.api.save_config(content, filename);
      if (result?.saved) notify(`配置已导出到：${result.path}`);
      else notify("已取消导出");
      return;
    }
    const blob = new Blob([content], { type: "application/json;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
    notify("配置已导出到浏览器下载目录");
  });
}

async function importConfig(event) {
  const file = event.target.files[0];
  if (!file) return;
  await withBusy("import-config", async () => {
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      throw new Error("配置文件不是有效的 JSON");
    }
    const result = await api("/api/config/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.config = result.config;
    normalizeRuleModes();
    activeRule.value = state.config.rules[0]?.id || "";
    notify("配置已导入，当前设备和通道选择已保留");
  });
  event.target.value = "";
}

function exitApplication() {
  withBusy("exit", async () => {
    await api("/api/exit", { method: "POST" });
    notify("正在停止输出并断开蓝牙...");
  });
}

function emergencyStop() {
  withBusy("stop", async () => {
    await api("/api/emergency-stop", { method: "POST" });
    notify("所有通道已停止");
  });
}

function simulate() {
  const selected = state.config.rules.find((rule) => rule.id === activeRule.value);
  const rule = selected?.enabled
    ? selected
    : state.config.rules.find((item) => item.enabled);
  if (!rule) return notify("没有启用的事件规则");
  withBusy("simulate", async () => {
    await api("/api/config", {
      method: "PUT",
      body: JSON.stringify(state.config),
    });
    await api("/api/simulate", {
      method: "POST",
      body: JSON.stringify({
        rule_id: rule.id,
        event_type: rule.event_type,
        tier: rule.tier,
        value: 10,
        message: rule.keyword || "测试弹幕",
      }),
    });
    notify(`已模拟 ${rule.name}`);
  });
}

function addDanmuRule() {
  const id = `danmu:custom:${Date.now()}`;
  state.config.rules.push({
    id,
    name: "新指定弹幕",
    event_type: "danmu",
    tier: "normal",
    keyword: "触发词",
    enabled: true,
    base_strength: 20,
    base_duration: 5,
    strength_rate: 0,
    duration_rate: 0,
    strength_limit: 100,
    duration_limit: 60,
    waveforms: ["潮汐"],
    play_mode: "loop",
    targets: [],
  });
  activeRule.value = id;
}

function removeRule(rule) {
  state.config.rules = state.config.rules.filter((item) => item.id !== rule.id);
}

function targetChecked(rule, deviceId, channel) {
  return rule.targets.some((item) => item.device_id === deviceId && item.channel === channel);
}

function toggleTarget(rule, deviceId, channel, checked) {
  rule.targets = rule.targets.filter((item) => !(item.device_id === deviceId && item.channel === channel));
  if (checked) rule.targets.push({ device_id: deviceId, channel });
}

function toggleWave(rule, name, checked) {
  if (checked) {
    if (!rule.waveforms.includes(name)) rule.waveforms.push(name);
  } else if (rule.waveforms.length > 1) {
    rule.waveforms = rule.waveforms.filter((item) => item !== name);
  }
  if (rule.waveforms.length === 1) rule.play_mode = "loop";
  if (rule.waveforms.length > 1 && rule.play_mode === "loop") {
    rule.play_mode = "sequence";
  }
}

async function importWaveform(event) {
  const file = event.target.files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  await withBusy("import", async () => {
    await api("/api/waveforms/import", { method: "POST", body: data });
    notify(`已导入 ${file.name}`);
  });
  event.target.value = "";
}

function waveOrder(rule, name) {
  const index = rule.waveforms.indexOf(name);
  return index >= 0 ? index + 1 : 0;
}

function displayTime(seconds) {
  return `${Math.max(0, seconds || 0).toFixed(1)}s`;
}

function displayStrength(value) {
  const strength = Math.max(0, Number(value) || 0);
  return Number.isInteger(strength) ? String(strength) : strength.toFixed(1);
}

function eventLabel(event) {
  return eventNames[event.event_type] || event.event_type;
}

function handleResize() {
  requestChartRender();
}

onMounted(() => {
  connectSocket();
  window.addEventListener("resize", handleResize);
});
onBeforeUnmount(() => {
  clearTimeout(reconnectTimer);
  if (chartFrame) cancelAnimationFrame(chartFrame);
  window.removeEventListener("resize", handleResize);
  socket?.close();
});
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark"><span></span><span></span><span></span></div>
        <div><strong>YCY</strong><small>LIVE PULSE</small></div>
      </div>
      <nav>
        <button :class="{ active: page === 'dashboard' }" @click="page = 'dashboard'">
          <i>◫</i><span>实时控制台</span>
        </button>
        <button :class="{ active: page === 'config' }" @click="page = 'config'">
          <i>⌁</i><span>事件配置</span>
        </button>
        <button :class="{ active: page === 'devices' }" @click="page = 'devices'">
          <i>⌁</i><span>设备管理</span>
        </button>
        <button :class="{ active: page === 'waves' }" @click="page = 'waves'">
          <i>∿</i><span>波形库</span>
        </button>
      </nav>
      <div class="side-status">
        <div><span class="dot" :class="{ on: state.listener.connected }"></span>B站监听</div>
        <b>{{ state.listener.connected ? "运行中" : "未连接" }}</b>
        <div><span class="dot" :class="{ on: connectedCount }"></span>设备连接</div>
        <b>{{ connectedCount }} / {{ state.devices.length }}</b>
      </div>
      <button class="emergency" @click="emergencyStop">■ 全通道急停</button>
      <button class="exit-app" :disabled="busy === 'exit'" @click="exitApplication">退出程序</button>
    </aside>

    <main>
      <header>
        <div>
          <p class="eyebrow">{{ page === "dashboard" ? "LIVE MONITOR" : "CONTROL CENTER" }}</p>
          <h1>{{ page === "dashboard" ? "直播脉冲控制台" : page === "config" ? "事件响应配置" : page === "devices" ? "蓝牙设备管理" : "波形资源库" }}</h1>
        </div>
        <div class="header-actions">
          <span class="clock">{{ new Date().toLocaleDateString("zh-CN") }}</span>
          <button class="ghost" @click="simulate">模拟事件</button>
          <button v-if="page === 'config'" class="ghost" @click="exportConfig">导出配置</button>
          <button v-if="page === 'config'" class="ghost" @click="configFileInput?.click()">导入配置</button>
          <button v-if="page === 'config'" class="primary" @click="saveConfig">保存配置</button>
          <input ref="configFileInput" class="hidden-file" type="file" accept=".json,application/json" @change="importConfig" />
        </div>
      </header>

      <section v-if="page === 'dashboard'" class="dashboard">
        <div class="live-strip panel">
          <div class="room-control">
            <span class="status-orb" :class="{ active: state.listener.connected }"></span>
            <div><small>直播间连接 · {{ state.listener.engine || "bilibili-api-python" }}</small><strong>{{ state.listener.connected ? `房间 ${state.listener.room_id}` : state.listener.connecting ? "正在连接..." : "等待连接" }}</strong></div>
            <input v-model="state.config.room_id" placeholder="输入B站房间号" />
            <button v-if="!state.listener.connected" class="primary" :disabled="state.listener.connecting || busy === 'listener:start'" @click="listenerAction('start')">{{ state.listener.connecting ? "连接中..." : "开始监听" }}</button>
            <button v-else class="ghost" @click="listenerAction('stop')">停止监听</button>
          </div>
          <div class="metrics">
            <div><small>已连接设备</small><strong>{{ connectedCount }}</strong></div>
            <div><small>本次事件</small><strong>{{ state.events.length }}</strong></div>
            <div><small>活动通道</small><strong>{{ state.devices.reduce((n,d) => n + ['A','B'].filter(c => d.outputs?.[c]?.strength > 0).length, 0) }}</strong></div>
          </div>
        </div>
        <div v-if="state.listener.error" class="listener-error">
          <strong>监听连接失败</strong>
          <span>{{ state.listener.error }}</span>
          <button @click="listenerAction('start')">重试</button>
        </div>

        <div class="dashboard-grid">
          <div class="device-grid">
            <article v-for="(device, index) in state.devices.slice(0, 4)" :key="device.id" class="device-card panel">
              <div class="card-title">
                <div><span class="device-index">0{{ index + 1 }}</span><h2>{{ device.name }}</h2></div>
                <span class="badge" :class="{ online: device.connected }">{{ device.connected ? `${device.generation}代 · 在线` : "离线" }}</span>
              </div>
              <canvas :ref="setChartCanvas" :data-device-id="device.id" class="wave-chart"></canvas>
              <div class="channel-row" v-for="channel in ['A','B']" :key="channel">
                <b :class="`channel-${channel.toLowerCase()}`">{{ channel }}</b>
                <div><small>实时强度</small><strong>{{ displayStrength(device.outputs?.[channel]?.strength) }}</strong></div>
                <div><small>当前波形</small><strong>{{ device.outputs?.[channel]?.waveform || "待机" }}</strong></div>
                <div><small>剩余时间</small><strong>{{ displayTime(device.outputs?.[channel]?.total_remaining) }}</strong></div>
                <div><small>频率 / 脉宽</small><strong>{{ device.outputs?.[channel]?.frequency || 0 }} / {{ device.outputs?.[channel]?.pulse_width || 0 }}</strong></div>
              </div>
            </article>
            <div v-if="!state.devices.length" class="empty panel">
              <div class="empty-wave">∿</div>
              <h2>还没有发现设备</h2>
              <p>前往设备管理扫描 UUID 为 FF30 的一代或二代设备。</p>
              <button class="primary" @click="page = 'devices'">管理设备</button>
            </div>
          </div>

          <aside class="event-feed panel">
            <div class="section-title"><div><small>EVENT STREAM</small><h2>实时事件</h2></div><span>{{ state.events.length }}</span></div>
            <div class="feed-list">
              <div v-for="(event, index) in state.events.slice(0, 30)" :key="`${event.timestamp}-${index}`" class="feed-item">
                <span class="event-icon" :class="event.event_type">{{ event.event_type === "gift" ? "◇" : event.event_type === "like" ? "♥" : "◌" }}</span>
                <div><strong>{{ event.username || "匿名用户" }}</strong><p>{{ eventLabel(event) }} {{ event.message }}</p></div>
                <b>{{ event.value || 1 }}</b>
              </div>
              <div v-if="!state.events.length" class="feed-empty">等待直播事件...</div>
            </div>
          </aside>
        </div>
      </section>

      <section v-else-if="page === 'devices'" class="content-page">
        <div class="page-toolbar panel">
          <div><h2>BLE 设备</h2><p>扫描服务 UUID FF30，支持同时连接多台设备。</p></div>
          <button class="primary" :disabled="busy === 'scan'" @click="scan">{{ busy === "scan" ? "扫描中..." : "扫描设备" }}</button>
        </div>
        <div class="manage-grid">
          <article v-for="device in state.devices" :key="device.id" class="manage-card panel">
            <div class="device-symbol">⌁</div>
            <div class="manage-info"><h3>{{ device.name }}</h3><code>{{ device.id }}</code><p v-if="device.error" class="error">{{ device.error }}</p></div>
            <label>产品代际<select v-model.number="device.generation" @change="setGeneration(device)"><option :value="1">一代</option><option :value="2">二代</option></select></label>
            <div class="battery">{{ device.battery == null ? "--" : `${device.battery}%` }}</div>
            <button v-if="!device.connected" class="primary" @click="deviceAction(device, 'connect')">连接</button>
            <button v-else class="ghost" @click="deviceAction(device, 'disconnect')">断开</button>
          </article>
          <div v-if="!state.devices.length" class="empty panel"><h2>点击扫描寻找设备</h2><p>请确认 Windows 蓝牙已开启，设备处于可连接状态。</p></div>
        </div>
        <p class="protocol-note">一代和二代使用相同服务 UUID，协议未提供代际查询命令。首次发现默认二代，选择后会按设备地址记忆。</p>
      </section>

      <section v-else-if="page === 'config'" class="config-layout">
        <div class="rule-nav panel">
          <div class="section-title"><div><small>AUTOMATION</small><h2>触发规则</h2></div></div>
          <template v-for="(rules, type) in groupedRules" :key="type">
            <p class="rule-group-title">{{ eventNames[type] }}</p>
            <button v-for="rule in rules" :key="rule.id" :class="{ active: activeRule === rule.id }" @click="activeRule = rule.id">
              <span class="dot" :class="{ on: rule.enabled }"></span><span>{{ rule.name }}</span><small>{{ unavailableEvents.has(rule.event_type) ? "暂无推送" : (tierNames[rule.tier] || "") }}</small>
            </button>
          </template>
          <button class="add-rule" @click="addDanmuRule">＋ 添加指定弹幕</button>
        </div>
        <div class="rule-editor panel">
          <template v-if="state.config.rules.find(r => r.id === activeRule) || state.config.rules[0]">
            <template v-for="rule in [state.config.rules.find(r => r.id === activeRule) || state.config.rules[0]]" :key="rule.id">
              <div class="editor-head">
                <div><p class="eyebrow">{{ eventNames[rule.event_type] }}</p><input class="title-input" v-model="rule.name" /></div>
                <label class="switch"><input type="checkbox" v-model="rule.enabled" :disabled="unavailableEvents.has(rule.event_type)" /><span></span>启用</label>
              </div>
              <div v-if="unavailableEvents.has(rule.event_type)" class="platform-warning">
                B站直播信息流目前不推送单个用户的{{ rule.event_type === "leave" ? "离开" : "取关" }}事件，因此该规则仅保留配置和模拟能力，不会在直播监听中自动触发。
              </div>
              <div class="form-grid">
                <label v-if="rule.event_type === 'danmu'">触发弹幕<input v-model="rule.keyword" placeholder="留空表示任意弹幕" /></label>
                <label v-if="['danmu','like','gift','enter','leave','follow','unfollow','share'].includes(rule.event_type)">用户身份<select v-model="rule.tier"><option v-for="(name,key) in tierNames" :key="key" :value="key">{{ name }}</option></select></label>
                <label>基础强度<input type="number" min="0" max="276" v-model.number="rule.base_strength" /></label>
                <label>基础时间（秒）<input type="number" min="0" step=".1" v-model.number="rule.base_duration" /></label>
                <label>强度增幅率<input type="number" min="0" step=".01" v-model.number="rule.strength_rate" /></label>
                <label>时间增幅率<input type="number" min="0" step=".01" v-model.number="rule.duration_rate" /></label>
                <label>强度上限<input type="number" min="0" max="276" v-model.number="rule.strength_limit" /></label>
                <label>时间上限（秒）<input type="number" min="0" step=".1" v-model.number="rule.duration_limit" /></label>
              </div>
              <div class="editor-section">
                <div class="subhead">
                  <div><h3>波形选择</h3><small>角标序号就是顺序播放次序</small></div>
                  <label v-if="rule.waveforms.length > 1">播放模式
                    <select v-model="rule.play_mode">
                      <option value="sequence">{{ modeNames.sequence }}</option>
                      <option value="random">{{ modeNames.random }}</option>
                    </select>
                  </label>
                  <span v-else class="single-wave-mode">单波形播放</span>
                </div>
                <div class="wave-checks">
                  <label v-for="wave in state.waveforms" :key="wave.name" :class="{ selected: rule.waveforms.includes(wave.name) }">
                    <input type="checkbox" :checked="rule.waveforms.includes(wave.name)" @change="toggleWave(rule, wave.name, $event.target.checked)" />
                    <span v-if="waveOrder(rule, wave.name)" class="wave-order">{{ waveOrder(rule, wave.name) }}</span>
                    <span class="mini-wave">∿</span><b>{{ wave.name }}</b><small>{{ wave.source }}</small>
                  </label>
                </div>
              </div>
              <div class="editor-section">
                <h3>输出设备与通道</h3>
                <div class="target-grid">
                  <div v-for="device in state.devices" :key="device.id">
                    <strong>{{ device.name }} <small>{{ device.generation }}代</small></strong>
                    <label v-for="channel in ['A','B']" :key="channel"><input type="checkbox" :checked="targetChecked(rule, device.id, channel)" @change="toggleTarget(rule, device.id, channel, $event.target.checked)" />{{ channel }} 通道</label>
                  </div>
                  <p v-if="!state.devices.length">扫描设备后可配置输出目标。</p>
                </div>
              </div>
              <button v-if="rule.id.includes(':custom:')" class="danger-link" @click="removeRule(rule)">删除这条规则</button>
            </template>
          </template>
        </div>
      </section>

      <section v-else class="content-page">
        <div class="page-toolbar panel">
          <div><h2>波形库</h2><p>支持役次元 JSON 和郊狼 PULSE 文件，最多读取前 100 组频率/脉宽。</p></div>
          <label class="primary upload">导入波形<input type="file" accept=".json,.pulse,.pules,.txt" @change="importWaveform" /></label>
        </div>
        <div class="wave-library">
          <article v-for="wave in state.waveforms" :key="wave.name" class="wave-card panel">
            <div class="wave-preview">∿∿∿</div><h3>{{ wave.name }}</h3><p>{{ wave.source }} · {{ wave.points.length }} 点</p>
            <div class="wave-bars"><i v-for="(point,index) in wave.points.slice(0,20)" :key="index" :style="{height: `${Math.max(4, point.pulse_width)}%`}"></i></div>
          </article>
        </div>
      </section>
    </main>
    <transition name="toast"><div v-if="toast" class="toast">{{ toast }}</div></transition>
  </div>
</template>
