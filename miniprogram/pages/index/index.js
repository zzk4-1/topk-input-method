const { getBase, setBase, request } = require('../../utils/api');

const SCENES = [
  '朋友约你周末吃饭',
  '和朋友约晚上看电影',
  '同事催你交方案，你在处理',
  '家人发消息关心你近况',
];

Page({
  data: {
    started: false,
    showScene: true,
    showSettings: false,
    scenes: SCENES,
    sceneInput: '',
    serverUrl: '',
    messages: [],
    draft: '',
    candidates: [],
    pending: false,
    replying: false,
    connected: true,
    scrollInto: '',
  },

  onLoad() {
    this.awaiting = false;   // 单飞标志：防轮询重入
    this.version = -1;       // 上次渲染的版本号
    this.setData({ serverUrl: getBase() });
    this.startPolling();
  },

  onUnload() {
    if (this.timer) clearInterval(this.timer);
  },

  startPolling() {
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => this.poll(), 800);
    this.poll();
  },

  poll() {
    if (this.awaiting) return;
    this.awaiting = true;
    request('/api/state')
      .then((st) => {
        const changed = st.version !== this.version;
        this.version = st.version;
        if (!changed) {
          this.setData({ connected: true });
          return;
        }
        this.setData({
          connected: true,
          started: st.started,
          messages: st.messages.map((m, i) => ({ role: m.role, text: m.text, _id: st.version + '-' + i })),
          draft: st.draft,
          candidates: st.candidates,
          pending: st.pending,
          replying: st.replying,
        });
        if (st.messages.length) {
          this.setData({ scrollInto: 'msg-' + (st.messages.length - 1) });
        }
      })
      .catch(() => this.setData({ connected: false }))
      .then(() => { this.awaiting = false; });
  },

  action(type, index) {
    if (!this.data.started || this.data.pending) return;
    this.setData({ pending: true }); // 乐观置灰，等轮询刷新
    request('/api/action', 'POST', { type: type, index: index || 0 })
      .then(() => this.poll())
      .catch(() => this.setData({ connected: false, pending: false }));
  },

  onCandidate(e) {
    this.action('select', Number(e.currentTarget.dataset.index));
  },

  onMore() {
    this.action('more');
  },

  onUndo() {
    this.action('undo');
  },

  onSend() {
    const draft = this.data.draft;
    if (!this.data.started || !draft || this.data.pending) return;
    // 发送即复制：用户切到微信聊天框长按粘贴
    wx.setClipboardData({
      data: draft,
      success() {
        wx.hideToast();
        wx.showToast({ title: '已复制，去微信粘贴', icon: 'none', duration: 2500 });
      },
    });
    this.setData({ pending: true });
    request('/api/action', 'POST', { type: 'send' })
      .then(() => this.poll())
      .catch(() => this.setData({ connected: false, pending: false }));
  },

  onCopy() {
    const draft = this.data.draft;
    if (!draft) return;
    wx.setClipboardData({
      data: draft,
      success() {
        wx.hideToast();
        wx.showToast({ title: '已复制', icon: 'none', duration: 1500 });
      },
    });
  },

  // ---- 场景 ----
  onSceneTap(e) {
    this.startScene(e.currentTarget.dataset.text);
  },

  onSceneInput(e) {
    this.setData({ sceneInput: e.detail.value });
  },

  onSceneCustom() {
    const t = (this.data.sceneInput || '').trim();
    if (t) this.startScene(t);
  },

  startScene(text) {
    this.setData({ pending: true });
    request('/api/start', 'POST', { text: text })
      .then(() => {
        this.version = -1; // 强制下一次渲染
        this.setData({ showScene: false, pending: true });
        this.poll();
      })
      .catch((err) => {
        const msg = (err && err.message ? err.message : '').slice(0, 80);
        wx.showModal({
          title: '连接失败',
          content: msg + '\n\n请求地址：' + getBase() + '/api/start\n\n若地址不对请到「服务器设置」修改',
          showCancel: false,
        });
        this.setData({ connected: false, pending: false });
      });
  },

  onReselectTap() {
    this.setData({ showScene: true });
  },

  // 场景面板里直接打开服务器设置（场景浮层保持打开，保存后回到场景面板）
  onOpenSettingsFromScene() {
    this.setData({ showSettings: true, serverUrl: getBase() });
  },

  // ---- 设置 ----
  onOpenSettings() {
    this.setData({ showSettings: true, serverUrl: getBase() });
  },

  onCloseSettings() {
    this.setData({ showSettings: false });
  },

  noop() {},

  onServerInput(e) {
    this.setData({ serverUrl: e.detail.value });
  },

  onSaveServer() {
    const r = setBase(this.data.serverUrl);
    if (r.error) {
      wx.showToast({ title: r.error, icon: 'none', duration: 2500 });
      return;
    }
    this.setData({ showSettings: false, serverUrl: r.base, connected: true });
    this.version = -1;
    this.startPolling();
  },
});
