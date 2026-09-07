/**
 * 本地题库管理面板 —— Alpine.js 3 单根组件（零构建）。
 *
 * 调用的后端接口（design.md §7，由管理 API 提供）：
 *   GET    /api/stats                            今日/累计统计 + 近 14 天序列
 *   GET    /api/log?limit=                       近期调用日志
 *   GET    /api/providers                        provider 列表
 *   POST   /api/providers                        新增
 *   PUT    /api/providers/{id}                   编辑/启停（api_key 留空 = 不修改）
 *   DELETE /api/providers/{id}                   删除
 *   POST   /api/providers/reorder                {ids:[...]} 按序重排优先级
 *   POST   /api/providers/{id}/test              {ok, latency_ms, reply, error}
 *   GET    /api/cache?page=&page_size=&q=&type=  分页 + 题目模糊搜索
 *   DELETE /api/cache/{id}                       单条删除
 *   POST   /api/cache/delete-batch               {ids:[...]} 批量删除
 *   GET    /api/cache/export                     导出全部缓存 JSON
 *   GET    /api/settings                         读设置
 *   PUT    /api/settings                         写设置（涉及路由的键热重建）
 *
 * 鉴权：开启 api_token 后所有管理请求携带 X-Token 头（顶栏输入框，localStorage 持久化）。
 */

const REFRESH_INTERVAL_MS = 30_000;
const LOG_LIMIT = 20;
const CACHE_PAGE_SIZE = 20;
const NOTICE_TIMEOUT_MS = 4000;
const TOKEN_STORAGE_KEY = 'tiku_token';
const TRUNCATE_LENGTH = 40;

const NUMERIC_SETTING_KEYS = [
  'ttl_days',
  'cleanup_batch_size',
  'cleanup_interval_hours',
  'llm_timeout',
  'num_retries',
  'allowed_fails',
  'cooldown_time',
];

const OCS_CONFIG = {
  url: 'http://127.0.0.1:8000/api/query',
  name: '本地题库',
  method: 'get',
  data: { title: '${title}', type: '${type}', options: '${options}' },
  handler: 'return (res) => res.code === 1 ? [res.question, res.answer] : undefined',
};

const OCS_CONFIG_JSON = JSON.stringify(OCS_CONFIG, null, 2);

const KIND_LABELS = {
  hit: '缓存命中',
  miss: '未命中',
  'llm-fail': 'LLM 失败',
  'parse-fail': '解析失败',
  import: '导入',
};

const QTYPE_LABELS = { single: '单选', multiple: '多选', judgement: '判断', completion: '填空' };

/** 宽容数字：任何非有限值归零，避免 NaN 渗入模板。 */
function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** 列表载荷归一：兼容裸数组或 {items|list|data: [...]} 包装。 */
function asArray(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== 'object') return [];
  const list = payload.items || payload.list || payload.data;
  return Array.isArray(list) ? list : [];
}

/** /api/stats 响应归一：容忍 today/total_stats/days 的字段别名。 */
function normalizeStats(raw) {
  const source = raw || {};
  const today = source.today || {};
  const all = source.total_stats || source.cumulative || {};
  const days = asArray(source.days || source.series).map((day) => ({
    day: String(day.day || day.date || ''),
    hits: num(day.cache_hits ?? day.hits),
    misses: num(day.cache_misses ?? day.misses),
  }));
  return {
    totalQuestions: num(source.total ?? source.total_questions),
    todayHits: num(today.cache_hits ?? today.hits),
    todayMisses: num(today.cache_misses ?? today.misses),
    todayLlmCalls: num(today.llm_calls),
    todayFailures: num(today.llm_failures),
    totalHits: num(all.cache_hits),
    totalLlmCalls: num(all.llm_calls),
    totalPromptTokens: num(all.prompt_tokens),
    totalCompletionTokens: num(all.completion_tokens),
    days,
  };
}

/** provider 行归一：SQLite 的 0/1 布尔与可空数值转成模板友好的形态。 */
function normalizeProvider(provider) {
  return {
    ...provider,
    priority: num(provider.priority ?? 1),
    enabled: provider.enabled === 1 || provider.enabled === true,
    rpm: provider.rpm ?? '',
    max_parallel: provider.max_parallel ?? '',
  };
}

/** 缓存行归一：时间戳与计数确保是数字。 */
function normalizeCacheItem(item) {
  return {
    ...item,
    hits: num(item.hits),
    created_at: num(item.created_at),
    last_hit_at: num(item.last_hit_at),
  };
}

/** 不可变交换列表两个下标（用于优先级上移/下移）。 */
function swapAt(list, from, to) {
  const copy = [...list];
  [copy[from], copy[to]] = [copy[to], copy[from]];
  return copy;
}

/** 从错误响应体提取人类可读信息（兼容 {msg|detail|message} 与纯文本）。 */
async function readErrorMessage(resp) {
  try {
    const data = await resp.json();
    if (typeof data === 'string') return data;
    return data.msg || data.detail || data.message || JSON.stringify(data);
  } catch {
    return resp.statusText || '未知错误';
  }
}

/** 剪贴板写入，兼容非安全上下文的降级方案。 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  document.body.appendChild(area);
  area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if (!copied) throw new Error('复制失败，请手动选中文本复制');
}

/** 文本截断（表格展示与 confirm 提示共用）。 */
function truncText(text, length = TRUNCATE_LENGTH) {
  const value = String(text ?? '');
  return value.length > length ? value.slice(0, length) + '…' : value;
}

/** 新增 provider 的空白表单。 */
function blankProviderForm() {
  return { name: '', base_url: '', model: '', api_key: '', rpm: '', max_parallel: '' };
}

/** 设置表单默认值（与后端 DEFAULTS 对齐）。 */
function blankSettingsForm() {
  return {
    ttl_days: 60,
    cleanup_batch_size: 500,
    cleanup_interval_hours: 6,
    llm_timeout: 20,
    num_retries: 1,
    allowed_fails: 3,
    cooldown_time: 60,
    routing_strategy: 'simple-shuffle',
    api_token: '',
  };
}

function tikuApp() {
  return {
    // ---------- 状态 ----------
    tab: 'dashboard',
    error: '',
    notice: '',
    token: '',
    ocsJson: OCS_CONFIG_JSON,

    stats: null,
    logItems: [],

    providers: [],
    providerModalOpen: false,
    providerEditing: null,
    providerForm: blankProviderForm(),
    testResult: null,

    cacheQ: '',
    cacheType: '',
    cachePage: 1,
    cachePageSize: CACHE_PAGE_SIZE,
    cacheItems: [],
    cacheTotal: 0,
    cacheBusy: false,
    selectedIds: [],

    settingsForm: blankSettingsForm(),
    settingsLoaded: false,
    settingsBusy: false,

    // ---------- 生命周期 ----------
    init() {
      this.token = localStorage.getItem(TOKEN_STORAGE_KEY) || '';
      this.switchTab('dashboard');
      window.setInterval(() => {
        if (this.tab === 'dashboard' && !document.hidden) this.refreshDashboard();
      }, REFRESH_INTERVAL_MS);
    },

    switchTab(tab) {
      this.tab = tab;
      if (tab === 'dashboard') this.refreshDashboard();
      if (tab === 'providers') this.loadProviders();
      if (tab === 'cache') this.loadCache();
      if (tab === 'settings') this.loadSettings();
    },

    // ---------- 统一请求与提示 ----------
    async api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      let body = options.body;
      if (body !== undefined && typeof body !== 'string') {
        body = JSON.stringify(body);
        headers['Content-Type'] = 'application/json';
      }
      if (this.token) headers['X-Token'] = this.token;

      let resp;
      try {
        resp = await fetch(path, { ...options, body, headers });
      } catch (err) {
        throw new Error('网络错误：无法连接服务（' + err.message + '）');
      }
      if (resp.status === 401) {
        throw new Error('需要 token：请在右上角填写访问令牌后重试');
      }
      if (!resp.ok) {
        throw new Error('请求失败（HTTP ' + resp.status + '）：' + (await readErrorMessage(resp)));
      }
      if (resp.status === 204) return null;
      return resp.json();
    },

    /** 包装一次异步操作：异常进顶部错误条，可选成功提示。 */
    async run(task, successMsg = '') {
      try {
        await task();
        if (successMsg) this.setNotice(successMsg);
      } catch (err) {
        this.setError(err.message);
      }
    },

    setError(message) {
      this.error = message;
    },

    setNotice(message) {
      this.notice = message;
      window.clearTimeout(this._noticeTimer);
      this._noticeTimer = window.setTimeout(() => {
        this.notice = '';
      }, NOTICE_TIMEOUT_MS);
    },

    saveToken() {
      localStorage.setItem(TOKEN_STORAGE_KEY, this.token);
      this.setNotice('访问令牌已保存，后续请求将携带');
    },

    // ---------- 仪表盘 ----------
    async refreshDashboard() {
      await this.run(async () => {
        const [stats, logs] = await Promise.all([
          this.api('/api/stats'),
          this.api('/api/log?limit=' + LOG_LIMIT),
        ]);
        this.stats = normalizeStats(stats);
        this.logItems = asArray(logs).map((item) => ({ ...item, ts: num(item.ts) }));
      });
    },

    todayHitRate() {
      if (!this.stats) return '—';
      const total = this.stats.todayHits + this.stats.todayMisses;
      return total > 0 ? Math.round((this.stats.todayHits / total) * 100) + '%' : '—';
    },

    chartMax() {
      if (!this.stats) return 1;
      const values = this.stats.days.map((day) => day.hits + day.misses);
      return Math.max(1, ...values);
    },

    barHeight(value) {
      return Math.max(2, Math.round((num(value) / this.chartMax()) * 100)) + '%';
    },

    // ---------- Provider 管理 ----------
    async loadProviders() {
      await this.run(async () => {
        const payload = await this.api('/api/providers');
        this.providers = asArray(payload)
          .map(normalizeProvider)
          .sort((a, b) => a.priority - b.priority);
      });
    },

    openProviderModal(provider) {
      this.providerEditing = provider || null;
      this.providerForm = provider
        ? {
            name: provider.name,
            base_url: provider.base_url,
            model: provider.model,
            api_key: '',
            rpm: provider.rpm ?? '',
            max_parallel: provider.max_parallel ?? '',
          }
        : blankProviderForm();
      this.providerModalOpen = true;
    },

    closeProviderModal() {
      this.providerModalOpen = false;
    },

    /** 组装 provider 提交体：可选字段空串归 null，enabled 透传。 */
    buildProviderBody(fields, enabled) {
      const optional = (value) => (value === '' || value === null || value === undefined ? null : num(value));
      return {
        name: String(fields.name || '').trim(),
        base_url: String(fields.base_url || '').trim(),
        model: String(fields.model || '').trim(),
        api_key: String(fields.api_key || '').trim(),
        rpm: optional(fields.rpm),
        max_parallel: optional(fields.max_parallel),
        enabled,
      };
    },

    async saveProvider() {
      const editing = this.providerEditing;
      const form = this.providerForm;
      if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
        this.setError('名称、Base URL、模型均为必填项');
        return;
      }
      const enabled = editing ? editing.enabled : true;
      const body = this.buildProviderBody(form, enabled);
      await this.run(async () => {
        if (editing) {
          await this.api('/api/providers/' + editing.id, { method: 'PUT', body });
        } else {
          await this.api('/api/providers', { method: 'POST', body });
        }
        this.providerModalOpen = false;
        await this.loadProviders();
      }, editing ? '已保存对「' + form.name + '」的修改' : '已新增 provider「' + form.name + '」');
    },

    async toggleProvider(provider) {
      const next = !provider.enabled;
      const body = this.buildProviderBody(provider, next);
      await this.run(async () => {
        await this.api('/api/providers/' + provider.id, { method: 'PUT', body });
        await this.loadProviders();
      }, next ? '已启用「' + provider.name + '」' : '已停用「' + provider.name + '」');
    },

    async removeProvider(provider) {
      const confirmed = window.confirm('确认删除 provider「' + provider.name + '」？此操作不可恢复。');
      if (!confirmed) return;
      await this.run(async () => {
        await this.api('/api/providers/' + provider.id, { method: 'DELETE' });
        await this.loadProviders();
      }, '已删除「' + provider.name + '」');
    },

    async moveProvider(index, direction) {
      const target = index + direction;
      if (target < 0 || target >= this.providers.length) return;
      const ids = swapAt(
        this.providers.map((provider) => provider.id),
        index,
        target,
      );
      await this.run(async () => {
        await this.api('/api/providers/reorder', { method: 'POST', body: { ids } });
        await this.loadProviders();
      }, '优先级已更新');
    },

    async testProvider(provider) {
      this.testResult = { name: provider.name, pending: true };
      await this.run(async () => {
        const result = await this.api('/api/providers/' + provider.id + '/test', { method: 'POST' });
        this.testResult = { name: provider.name, pending: false, ...result };
      });
    },

    // ---------- 缓存管理 ----------
    async loadCache() {
      this.cacheBusy = true;
      try {
        const params = new URLSearchParams({
          page: String(this.cachePage),
          page_size: String(this.cachePageSize),
        });
        if (this.cacheQ.trim()) params.set('q', this.cacheQ.trim());
        if (this.cacheType) params.set('type', this.cacheType);
        const payload = await this.api('/api/cache?' + params.toString());
        const items = asArray(payload);
        this.cacheItems = items.map(normalizeCacheItem);
        this.cacheTotal = num(payload?.total ?? items.length);
        this.pruneSelection();
      } catch (err) {
        this.setError(err.message);
      } finally {
        this.cacheBusy = false;
      }
    },

    /** 翻页/删除后清掉不在当前页的勾选，避免对不可见行误操作。 */
    pruneSelection() {
      const pageIds = new Set(this.cacheItems.map((item) => item.id));
      this.selectedIds = this.selectedIds.filter((id) => pageIds.has(id));
    },

    cacheTotalPages() {
      return Math.max(1, Math.ceil(this.cacheTotal / this.cachePageSize));
    },

    async searchCache() {
      this.cachePage = 1;
      await this.loadCache();
    },

    async changePage(delta) {
      const next = this.cachePage + delta;
      if (next < 1 || next > this.cacheTotalPages()) return;
      this.cachePage = next;
      await this.loadCache();
    },

    isSelected(id) {
      return this.selectedIds.includes(id);
    },

    allPageSelected() {
      return this.cacheItems.length > 0 && this.cacheItems.every((item) => this.isSelected(item.id));
    },

    toggleSelect(id) {
      this.selectedIds = this.isSelected(id)
        ? this.selectedIds.filter((selected) => selected !== id)
        : [...this.selectedIds, id];
    },

    toggleSelectAll() {
      this.selectedIds = this.allPageSelected() ? [] : this.cacheItems.map((item) => item.id);
    },

    async deleteCacheItem(item) {
      const confirmed = window.confirm('确认删除这条缓存？\n' + truncText(item.question));
      if (!confirmed) return;
      await this.run(async () => {
        await this.api('/api/cache/' + item.id, { method: 'DELETE' });
        await this.loadCache();
      }, '已删除 1 条缓存');
    },

    async deleteSelected() {
      if (this.selectedIds.length === 0) {
        this.setNotice('请先勾选要删除的缓存条目');
        return;
      }
      const count = this.selectedIds.length;
      const confirmed = window.confirm('确认删除所选 ' + count + ' 条缓存？此操作不可恢复。');
      if (!confirmed) return;
      await this.run(async () => {
        await this.api('/api/cache/delete-batch', {
          method: 'POST',
          body: { ids: [...this.selectedIds] },
        });
        this.selectedIds = [];
        this.cachePage = 1;
        await this.loadCache();
      }, '已删除 ' + count + ' 条缓存');
    },

    exportCache() {
      const suffix = this.token ? '?token=' + encodeURIComponent(this.token) : '';
      window.open('/api/cache/export' + suffix, '_blank');
    },

    // ---------- 设置 ----------
    async loadSettings() {
      await this.run(async () => {
        const data = await this.api('/api/settings');
        const form = blankSettingsForm();
        for (const key of Object.keys(form)) {
          if (data[key] !== undefined && data[key] !== null) form[key] = data[key];
        }
        this.settingsForm = form;
        this.settingsLoaded = true;
      });
    },

    async saveSettings() {
      this.settingsBusy = true;
      try {
        const body = {};
        for (const key of NUMERIC_SETTING_KEYS) body[key] = num(this.settingsForm[key]);
        body.routing_strategy = this.settingsForm.routing_strategy;
        body.api_token = String(this.settingsForm.api_token ?? '').trim();
        await this.api('/api/settings', { method: 'PUT', body });
        this.setNotice('设置已保存，涉及路由的修改已热重建生效');
      } catch (err) {
        this.setError(err.message);
      } finally {
        this.settingsBusy = false;
      }
    },

    // ---------- OCS 配置指引 ----------
    async copyOcs() {
      try {
        await copyText(this.ocsJson);
        this.setNotice('OCS 配置 JSON 已复制到剪贴板');
      } catch (err) {
        this.setError(err.message);
      }
    },

    // ---------- 展示辅助 ----------
    kindLabel(kind) {
      return KIND_LABELS[kind] || kind;
    },

    badgeClass(kind) {
      if (kind === 'hit') return 'badge badge-hit';
      if (kind === 'miss') return 'badge badge-miss';
      if (kind === 'llm-fail' || kind === 'parse-fail') return 'badge badge-fail';
      return 'badge badge-import';
    },

    qtypeLabel(qtype) {
      return QTYPE_LABELS[qtype] || qtype;
    },

    fmtTs(ts) {
      return ts ? new Date(ts * 1000).toLocaleString() : '—';
    },

    fmtDay(day) {
      return day.length >= 10 ? day.slice(5) : day;
    },
  };
}

