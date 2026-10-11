// 服务器地址管理 + 请求封装
const KEY = 'topk_server';
const DEFAULT_BASE = 'http://127.0.0.1:8000';

function cleanUrl(url) {
  // 清洗常见误输：反引号、引号、空白、零宽字符（复制粘贴常带）、全角冒号
  return String(url || '')
    .replace(/[\u200B-\u200D\uFEFF]/g, '')
    .replace(/[`'"\s]/g, '')
    .replace(/：/g, ':');
}

function isValidBase(url) {
  return /^https?:\/\/[a-zA-Z0-9.\-]+(:\d+)?$/.test(url);
}

function getBase() {
  const cleaned = cleanUrl(wx.getStorageSync(KEY));
  return isValidBase(cleaned) ? cleaned : DEFAULT_BASE;
}

function setBase(url) {
  url = cleanUrl(url);
  if (url && !isValidBase(url)) {
    return { error: '格式应为 http://IP:端口' };
  }
  if (url) wx.setStorageSync(KEY, url);
  return { base: getBase() };
}

function request(path, method, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: getBase() + path,
      method: method || 'GET',
      data: data || {},
      timeout: 10000,
      success(res) {
        if (res.statusCode === 200) resolve(res.data);
        else reject(new Error('HTTP ' + res.statusCode));
      },
      fail(err) {
        reject(new Error(err.errMsg || 'request failed'));
      },
    });
  });
}

module.exports = { getBase, setBase, request };
