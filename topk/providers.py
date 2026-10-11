"""候选提供者层。

架构：CandidateProvider 统一接口
  DeepSeekProvider  LLM 在线生成（OpenAI 兼容协议，JSON 输出）
  CachingProvider   装饰器：磁盘缓存
  PrefetchProvider  装饰器：后台预取“一步之内”的下一屏结果，消除点选延迟
  SafeProvider      装饰器：异常时退回 StaticProvider 离线兜底，保证永远可用
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from urllib import request as urlrequest

from .cache import DiskCache


@dataclass
class Candidate:
    """一个候选短语：text 是对当前前缀的续写。"""

    text: str

    def to_json(self) -> dict:
        return {"text": self.text}


class CandidateProvider:
    """所有候选来源的统一接口。"""

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        raise NotImplementedError


def append_to_context(context: str, msg: str) -> str:
    """发送消息后的场景上文。session.commit 与预取共用，保证缓存键严格一致。"""
    if not msg:
        return context
    return f"{context}\n我：{msg}" if context else f"我：{msg}"


# ---------------------------------------------------------------- 离线来源

_FALLBACK_POOL: "list[str]" = [
    "好的，没问题",
    "嗯嗯，可以",
    "今晚有事，去不了",
    "下次一定",
    "吃什么？",
    "在哪儿见？",
    "几点？",
    "我快到了",
    "稍等一下",
    "谢谢！",
]
_PUNCT_POOL: "list[str]" = ["。", "？", "！", "，"]


class StaticProvider(CandidateProvider):
    """离线兜底：网络或 API 异常时保证输入法始终可用。"""

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        pool = (_PUNCT_POOL + _FALLBACK_POOL) if draft else _FALLBACK_POOL
        offset = (batch * 3) % len(pool)
        pool = pool[offset:] + pool[:offset]
        return [Candidate(t) for t in pool[:k]]


# ---------------------------------------------------------------- 在线来源

class DeepSeekProvider(CandidateProvider):
    """基于 DeepSeek Chat API 的候选生成（OpenAI 兼容协议，强制 JSON 输出）。"""

    API_URL = "https://api.deepseek.com/chat/completions"

    # 高情商表达原则参考开源项目 goutoujunshi（github.com/shengjidaguai-china/goutoujunshi，MIT）
    _SYSTEM_TMPL = """你是一个为肢体/语言障碍人士设计的智能输入法候选生成器。用户无法打字，只能点击屏幕上的候选短语来组句，候选质量直接决定沟通效率。

任务：根据【场景】和【已输入】前缀，预测用户接下来最可能想输入的内容，给出 {k} 个候选短语。

规则：
1. text 是对【已输入】的续写短语，不要重复已输入的内容；长度 1~12 个字，符合日常口语。可以只是一个词、一个片段，也可以直接拼成完整的一句话。
2. 短语要按聊天软件里原本的样子带标点：句中停顿用“，”，问句末尾带“？”，感叹末尾带“！”，陈述收尾可带“。”，例如“好的，没问题”“几点？”“今晚有事，去不了”。候选“！”“？”“。”这类续接标点也可以。
3. 表情：表达完整语气、适合作为句子收尾的候选，可以在末尾带上 1 个贴切的 emoji（如 😄👍🙏🥺🍳），让消息更生动；作为句子中间续接的短语（如“我想说”“今晚”）不要带。每批 {k} 个候选里带 emoji 的控制在 2~3 个，用户想选不带表情的表达时也要有得选。
4. 候选之间要覆盖不同的意图方向（例如 共情安慰/同意/拒绝/提问/改约/表达感受），按可能性从高到低排序。
5. 高情商表达：先接住对方的情绪，再表达自己。当对方消息带情绪（吐槽、难过、兴奋、为难等）时，至少给出 1 个以共情开头的候选（如“辛苦了，”“这确实挺难的，”“哈哈太好了，”）；每条候选只承载一个主动作，不把安慰、邀约、解释堆进同一条；语气温暖真实、站在自己立场但给对方留余地，不讨好也不硬刚。
6. 只输出如下格式的 JSON，不要输出任何其他内容：
{"candidates":[{"text":"短语"}]}"""

    def __init__(self, api_key: str, model: str = "deepseek-chat", timeout: float = 30.0):
        self.api_key = api_key.strip()
        self.model = model
        self.timeout = timeout

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        user_msg = (
            f"【场景】{context.strip() or '（未设置，按日常聊天理解）'}\n"
            f"【已输入】{draft if draft else '（空，请预测整句的开头）'}\n"
            f"请给出 {k} 个候选。"
        )
        if batch > 0:
            user_msg += f"\n注意：这是第 {batch + 1} 批候选，请给出之前批次未覆盖的其他表达方向。"
        content = self._chat([
            {"role": "system", "content": self._SYSTEM_TMPL.replace("{k}", str(k))},
            {"role": "user", "content": user_msg},
        ])
        return self._parse(content, k)

    def _chat(self, messages: list, max_tokens: int = 400, json_mode: bool = True) -> str:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.9,
            "max_tokens": max_tokens,  # 候选均为 ≤12 字片段，400 token 足够，缩短生成尾延迟
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urlrequest.Request(
            self.API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(content: str, k: int) -> "list[Candidate]":
        text = re.sub(r"```(?:json)?|```", "", content).strip()
        data = json.loads(text)
        items = data.get("candidates")
        if not isinstance(items, list) or not items:
            raise ValueError("响应缺少 candidates 字段")
        out: "list[Candidate]" = []
        seen: set = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            frag = str(item.get("text", "")).strip()
            if not frag or frag in seen:
                continue
            seen.add(frag)
            out.append(Candidate(frag))
            if len(out) >= k:
                break
        if not out:
            raise ValueError("响应中没有有效候选")
        return out

    # ---- 对方自动回复（网页 demo 用） ----
    # 高情商回复原则参考开源项目 goutoujunshi（github.com/shengjidaguai-china/goutoujunshi，MIT）
    _REPLY_SYSTEM = ("你在聊天软件里扮演用户的朋友。根据【聊天记录】，用一句自然的口语回复用户刚发出的消息，"
                     "4~25 个字。高情商原则：先接住用户消息里的情绪（认可感受、给情绪一个落点，如先说“辛苦了”“懂”），"
                     "再自然表达你的想法或给出一个可执行的小建议；一条消息只承载一个主动作，不堆叠安慰、邀约和解释；"
                     "语气温暖、清醒、口语化。并在句中或句尾配上 1~2 个贴切的 emoji 表情，emoji 不要超过 2 个，不要堆砌。"
                     "只输出这句话本身：不要引号、不要前缀、不要解释。")

    def get_reply(self, context: str) -> str:
        content = self._chat([
            {"role": "system", "content": self._REPLY_SYSTEM},
            {"role": "user", "content": f"【聊天记录】\n{context or '（新对话）'}"},
        ], max_tokens=96, json_mode=False)
        return content.strip().strip("“”\"'「」『』")


# ---------------------------------------------------------------- 装饰器

class CachingProvider(CandidateProvider):
    """磁盘缓存装饰器：命中则不调用内部提供者。"""

    def __init__(self, inner: CandidateProvider, cache: DiskCache):
        self.inner = inner
        self.cache = cache

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        hit = self.cache.get(context, draft, k, batch)
        if hit is not None:
            # 旧缓存条目可能带 complete 字段，读取时忽略
            return [Candidate(text=c["text"]) for c in hit]
        cands = self.inner.get_candidates(context, draft, k, batch)
        self.cache.put(context, draft, k, batch, [c.to_json() for c in cands])
        return cands


class SafeProvider(CandidateProvider):
    """异常兜底装饰器：内部提供者失败时退回离线候选，并记录 last_error 供 UI 提示。"""

    def __init__(self, inner: CandidateProvider, fallback: "CandidateProvider | None" = None):
        self.inner = inner
        self.fallback = fallback or StaticProvider()
        self.last_error: "str | None" = None

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        try:
            return self.inner.get_candidates(context, draft, k, batch)
        except Exception as exc:  # 网络/API/解析异常一律兜底
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.fallback.get_candidates(context, draft, k, batch)


class PrefetchProvider(CandidateProvider):
    """预取装饰器：当前屏就绪后，后台并发预取未来几步可能到达的候选屏。

    策略（两步级联）：
      第 1 步：点选前 limit 个候选后的下一屏 + 按 s 提交后的下一屏；
      第 2 步：上述每个子屏就绪后，继续级联预取其前 (limit-1) 个候选的孙屏。
    并发由信号量(workers)统一限流，多余任务排队等待；全部为 daemon 线程，
    程序退出立即终止、不阻塞。结果经内部 CachingProvider 落盘，
    用户真正点选时直接命中，感知延迟≈0。预取失败静默忽略。
    """

    def __init__(self, inner: CandidateProvider, cache: "DiskCache | None" = None,
                 limit: int = 3, depth: int = 2, workers: int = 6):
        self.inner = inner
        self.cache = cache                  # 用于跳过已缓存状态；可为 None
        self.limit = max(0, limit)          # 每层预取的候选分支数
        self.depth = max(0, depth)          # 前瞻步数
        self._sem = threading.Semaphore(max(2, workers))  # 并发调用上限
        self._inflight: set = set()         # 已排队/执行中的缓存键，防止重复预取
        self._lock = threading.Lock()
        self.prefetch_done = 0              # 累计完成的后台预取次数（供 UI 提示）

    def get_candidates(self, context: str, draft: str, k: int = 8, batch: int = 0) -> "list[Candidate]":
        cands = self.inner.get_candidates(context, draft, k, batch)
        if self.limit > 0 and self.depth > 0:
            for cand in cands[: self.limit]:
                self._schedule(context, draft + cand.text, k, self.depth)
            if draft:
                # 提交(s)后的下一屏：只预取这一层，不再从空前缀级联
                self._schedule(append_to_context(context, draft), "", k, 1)
        return cands

    def _schedule(self, context: str, child_draft: str, k: int, depth: int) -> None:
        if self.cache is not None and self.cache.get(context, child_draft, k, 0) is not None:
            return  # 已在缓存，点选时本来就直接命中
        key = DiskCache.make_key(context, child_draft, k, 0)
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        threading.Thread(
            target=self._prefetch, args=(context, child_draft, k, key, depth),
            name="topk-prefetch", daemon=True,
        ).start()

    def _prefetch(self, context: str, child_draft: str, k: int, key: str, depth: int) -> None:
        try:
            with self._sem:  # 限流：并发调用不超过 workers
                cands = self.inner.get_candidates(context, child_draft, k, 0)
            self.prefetch_done += 1
            if depth > 1:
                fanout = max(1, self.limit - 1)  # 第二层分支收窄，控制调用量
                for cand in cands[:fanout]:
                    self._schedule(context, child_draft + cand.text, k, depth - 1)
        except Exception:
            pass  # 预取失败不打扰用户；点选时正式路径会重新请求
        finally:
            with self._lock:
                self._inflight.discard(key)
