"""Top-K 输入法 · 控制台版交互。

用法：
  python3 main.py --context "朋友问你：今晚一起吃饭吗"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .cache import DiskCache
from .providers import (
    CachingProvider,
    DeepSeekProvider,
    PrefetchProvider,
    SafeProvider,
    StaticProvider,
)
from .session import InputSession

ROOT = Path(__file__).resolve().parent.parent

HELP = """\
说明：
- 每个候选都是对“已输入”的续写短语：
    点选后拼进前缀继续组句，可以一步步拼出完整的句子，也可以直接选中整句发送。
- 推荐流程：点选最接近意图的短语；没有中意的就按 m 换一批。
- 每次发送后，消息自动并入场景，作为下一轮预测的上文；对方的新消息用 e 更新进场景。
- u 可逐步撤销点选；候选结果会缓存，重复操作不重复计费。
- 后台预取：每屏就绪后并发预取“点选候选 / 按 s 发送”之后最多两步的候选屏，
  连续点选几乎零等待；分支/步数/并发用 --prefetch / --prefetch-depth /
  --prefetch-workers 调整（默认 3 / 2 / 6，--prefetch 0 关闭）。"""


def _clear() -> None:
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def build_provider(name: str, key_file: Path, use_cache: bool,
                   prefetch: int = 3, prefetch_depth: int = 2, prefetch_workers: int = 6):
    cache = DiskCache(ROOT / ".topk_cache.json") if use_cache else None
    engine = DeepSeekProvider(key_file.read_text(encoding="utf-8").strip())  # 仅程序运行时读取密钥
    inner = CachingProvider(engine, cache) if cache is not None else engine
    if prefetch > 0 and prefetch_depth > 0 and cache is not None:  # 预取依赖缓存落盘，否则结果会被丢弃
        inner = PrefetchProvider(inner, cache, limit=prefetch,
                                 depth=prefetch_depth, workers=prefetch_workers)
    return SafeProvider(inner, StaticProvider())


def render(session: InputSession) -> str:
    lines = [
        "=============== Top-K 输入法（控制台版）===============",
        f"场景　：{session.context or '（未设置，按 e 设置）'}",
        f"已输入：{session.draft or '∅'}",
        "-" * 56,
    ]
    for i, cand in enumerate(session.candidates, 1):
        lines.append(f" {i}. {cand.text}")
    lines.append("-" * 56)
    lines.append("命令：序号=点选 | m=换一批 | u=撤销 | e=设场景 | s=发送 | h=帮助 | q=退出")
    return "\n".join(lines)


def _pause(text: str = "按回车继续…") -> None:
    try:
        input(text)
    except EOFError:
        raise SystemExit(0)


def main(argv: "list[str] | None" = None) -> None:
    parser = argparse.ArgumentParser(description="Top-K 智能输入法 · 控制台版")
    parser.add_argument("--provider", choices=["api"], default="api",
                        help="候选来源：DeepSeek 在线预测（默认）")
    parser.add_argument("--context", default="", help="初始场景/上文")
    parser.add_argument("--k", type=int, default=8, help="每批候选数量（默认 8）")
    parser.add_argument("--key-file", default=str(ROOT / "key.txt"),
                        help="DeepSeek API Key 文件路径（默认 key.txt，仅程序运行时读取）")
    parser.add_argument("--no-cache", action="store_true", help="禁用磁盘缓存")
    parser.add_argument("--prefetch", type=int, default=3,
                        help="每屏后台预取的候选分支数（默认 3，0 关闭；依赖磁盘缓存）")
    parser.add_argument("--prefetch-depth", type=int, default=2,
                        help="预取前瞻步数（默认 2，连点两步都快；1=只预取下一屏）")
    parser.add_argument("--prefetch-workers", type=int, default=6,
                        help="预取并发请求上限（默认 6）")
    args = parser.parse_args(argv)

    provider = build_provider(args.provider, Path(args.key_file), not args.no_cache,
                              args.prefetch, args.prefetch_depth, args.prefetch_workers)
    session = InputSession(provider, context=args.context, k=args.k)

    while True:
        _clear()
        cands = session.candidates          # 懒刷新：此时才可能触发 LLM 调用
        err = getattr(provider, "last_error", None)
        if err:
            print(f"⚠ 预测引擎异常，已切换离线兜底候选：{err}\n")
            provider.last_error = None
        print(render(session))
        prefetched = getattr(provider, "prefetch_done", 0)
        if prefetched:
            print(f"(后台预取：{prefetched} 个下一步候选已就绪，点选将直接命中缓存)")

        try:
            cmd = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(cands):
                session.select(idx)
            else:
                _pause(f"没有 {cmd} 号候选，按回车继续…")
        elif cmd == "m":
            session.more()
        elif cmd == "u":
            if not session.undo():
                _pause("已经在最开头了，按回车继续…")
        elif cmd == "e":
            print(f"当前场景：{session.context or '（空）'}")
            try:
                session.set_context(input("输入新场景（直接回车保持不变）："))
            except EOFError:
                raise SystemExit(0)
        elif cmd == "s":
            msg = session.commit()
            print(f"[已发送] {msg}" if msg else "[空消息，未发送]")
            _pause()
        elif cmd == "h":
            print(HELP)
            _pause()
        elif cmd == "q":
            return
