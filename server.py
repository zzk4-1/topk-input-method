"""网页版 Demo 服务器：模拟手机里“微信式”聊天 + 大按钮 Top-K 候选。

复用 topk 全部核心（缓存 / 两步并发预取 / 离线兜底），新增：
  1. 发送消息后，对方（LLM 扮演）自动回复；
  2. 候选按钮 k=6（2×3 大按钮网格），全部大尺寸便于点按。

  py -3 server.py --open      # 启动 + 自动打开浏览器
然后浏览器访问 http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from topk.cli import build_provider
from topk.session import InputSession

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"


def unwrap_engine(provider):
    """剥开装饰器链，拿到底层引擎（DeepSeekProvider）。"""
    while hasattr(provider, "inner"):
        provider = provider.inner
    return provider


class ChatDemo:
    """把 InputSession 包装成聊天状态机：消息流 + 对方自动回复 + 候选预热。

    pending=True 表示候选区正在刷新（对方输入中 / 换批中），前端禁用按钮。
    所有 LLM 调用都在锁外执行，回填时用 candidate_key 比对，状态已变则丢弃。
    """

    def __init__(self, provider, replier, k: int = 6):
        self.provider = provider
        self.replier = replier
        self.k = k
        self.session = InputSession(provider, k=k)
        self.messages: "list[dict]" = []
        self.pending = False
        self.version = 0
        self._lock = threading.Lock()
        self._replying = False

    # ---- 内部 ----
    def _touch(self) -> None:
        self.version += 1

    def _context_text(self) -> str:
        return "\n".join(
            f"{'对方' if m['role'] == 'friend' else '我'}：{m['text']}" for m in self.messages
        )

    def _warm(self) -> None:
        """阻塞拉取当前状态的候选（通常命中预取缓存，瞬时返回）。"""
        key = self.session.candidate_key()
        try:
            cands = self.provider.get_candidates(*key)
        except Exception:
            cands = []
        with self._lock:
            # 状态已变（例如发送预热与回复预热并发）则丢弃结果，
            # 且不能误清 pending——由真正对应当前状态的预热负责收尾
            if self.session.apply_warmed(key, cands):
                self.pending = False
            self._touch()

    def _kick_reply(self) -> None:
        if self._replying:
            return
        self._replying = True
        threading.Thread(target=self._reply_worker, daemon=True, name="topk-reply").start()

    def _reply_worker(self) -> None:
        """对方“正在输入…”→ 生成回复 → 并入场景 → 预热新候选（全程保持“正在输入”）。"""
        try:
            time.sleep(random.uniform(1.2, 2.4))
            with self._lock:
                ctx = self._context_text()
            try:
                reply = (self.replier.get_reply(ctx) or "").strip()
            except Exception:
                reply = ""  # 回复失败不阻塞聊天，用户可继续输入
            if reply:
                with self._lock:
                    self.messages.append({"role": "friend", "text": reply})
                    self.session.set_context(self._context_text())
                    self.pending = True
                    self._touch()
                self._warm()  # 新候选就绪后，finally 统一结束“正在输入”
        finally:
            with self._lock:
                self._replying = False
                self._touch()

    # ---- 对外 API ----
    def start(self, friend_text: str) -> dict:
        with self._lock:
            self.messages = [{"role": "friend", "text": friend_text.strip() or "在吗？"}]
            self.session = InputSession(self.provider, context=self._context_text(), k=self.k)
            self.pending = True
            self._touch()
        self._warm()
        return self.snapshot()

    def action(self, kind: str, index: int = 0) -> dict:
        run = False
        with self._lock:
            if kind == "select":
                cands = self.session.ready_candidates()
                if cands and 0 <= index < len(cands):
                    self.session.select(index)
                    self.pending = True
                    self._touch()
                    run = True
            elif kind == "undo":
                if self.session.ready_candidates() and self.session.undo():
                    self.pending = True
                    self._touch()
                    run = True
            elif kind == "more":
                if self.session.ready_candidates():
                    self.session.more()
                    self.pending = True
                    self._touch()
                    run = True
            elif kind == "send":
                if self.session.draft:
                    msg = self.session.commit()
                    self.messages.append({"role": "me", "text": msg})
                    self._kick_reply()
                self.pending = True
                self._touch()
                run = True
        if run:
            self._warm()
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            cands = self.session.ready_candidates()
            err = getattr(self.provider, "last_error", None)
            self.provider.last_error = None  # 读后即清：作为一次性通知下发给前端
            return {
                "version": self.version,
                "started": bool(self.messages),
                "pending": self.pending,
                "replying": self._replying,
                "messages": list(self.messages),
                "draft": self.session.draft,
                "candidates": [] if (self.pending or not cands) else [c.text for c in cands],
                "prefetched": getattr(self.provider, "prefetch_done", 0),
                "error": err,
            }


class Handler(BaseHTTPRequestHandler):
    demo: ChatDemo = None  # main() 里注入

    def _send_json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif self.path == "/api/state":
            self._send_json(self.demo.snapshot())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if self.path == "/api/start":
                self._send_json(self.demo.start(str(body.get("text", ""))))
            elif self.path == "/api/action":
                self._send_json(self.demo.action(str(body.get("type", "")), int(body.get("index", 0))))
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, fmt, *args):
        pass  # 安静模式


def main() -> None:
    parser = argparse.ArgumentParser(description="Top-K 输入法 · 网页版 Demo（模拟手机聊天）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（真机预览用 0.0.0.0，手机经局域网访问）")
    parser.add_argument("--k", type=int, default=6, help="候选按钮数（网页版默认 6，2×3 大按钮）")
    parser.add_argument("--key-file", default=str(ROOT / "key.txt"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--prefetch", type=int, default=3)
    parser.add_argument("--prefetch-depth", type=int, default=2)
    parser.add_argument("--prefetch-workers", type=int, default=6)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    provider = build_provider("api", Path(args.key_file), not args.no_cache,
                              args.prefetch, args.prefetch_depth, args.prefetch_workers)
    demo = ChatDemo(provider, unwrap_engine(provider), k=args.k)
    Handler.demo = demo
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Top-K 输入法网页 Demo：{url}  (provider=api, k={args.k})", flush=True)
    if args.host not in ("127.0.0.1", "localhost"):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except OSError:
            lan_ip = "<本机局域网IP>"
        print(f"真机预览请在小程序设置中填：http://{lan_ip}:{args.port}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
