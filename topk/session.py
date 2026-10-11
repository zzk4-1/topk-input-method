"""输入会话状态机：场景上下文、当前前缀、撤销栈与候选的懒刷新。

UI 无关——控制台版、将来的 PC 图形界面与网页版都只操作这一层。
"""
from __future__ import annotations

from .providers import Candidate, CandidateProvider, append_to_context


class InputSession:
    def __init__(self, provider: CandidateProvider, context: str = "", k: int = 8):
        self.provider = provider
        self.context = context          # 场景/对话上文（发送的消息会自动并入）
        self.k = k                      # 每批候选数量（控制台 8；将来大按钮界面可取 6 或 9）
        self.draft = ""                 # 当前已确定的输入前缀
        self._undo_stack: "list[str]" = []
        self._batch = 0                 # “换一批”计数，参与缓存键
        self._candidates: "list[Candidate] | None" = None

    # ---- 候选（懒加载：只有界面需要展示时才真正调用预测引擎） ----
    @property
    def candidates(self) -> "list[Candidate]":
        if self._candidates is None:
            self._candidates = self.provider.get_candidates(
                self.context, self.draft, self.k, self._batch
            )
        return self._candidates

    def _invalidate(self) -> None:
        self._candidates = None

    # ---- 供非阻塞 UI / 后台预热使用 ----
    def ready_candidates(self) -> "list[Candidate] | None":
        """已就绪的候选（绝不触发预测调用）；None 表示尚未就绪。"""
        return self._candidates

    def candidate_key(self) -> tuple:
        """当前候选对应的状态键，供后台预热后比对。"""
        return (self.context, self.draft, self.k, self._batch)

    def apply_warmed(self, key: tuple, cands: "list[Candidate]") -> bool:
        """后台预热结果回填：仅当状态未变时生效，返回是否生效。"""
        if self._candidates is None and self.candidate_key() == key:
            self._candidates = cands
            return True
        return False

    # ---- 用户动作 ----
    def select(self, index: int) -> Candidate:
        """点选第 index 个候选：前缀生长，重新预测。"""
        cand = self.candidates[index]
        self._undo_stack.append(self.draft)
        self.draft += cand.text
        self._batch = 0
        self._invalidate()
        return cand

    def more(self) -> None:
        """换一批（重新采样不同方向）。"""
        self._batch += 1
        self._invalidate()

    def undo(self) -> bool:
        """撤销上一次点选。"""
        if not self._undo_stack:
            return False
        self.draft = self._undo_stack.pop()
        self._batch = 0
        self._invalidate()
        return True

    def commit(self) -> str:
        """发送当前前缀：清空草稿，并把消息并入场景作为下一轮预测的上文。"""
        msg, self.draft = self.draft, ""
        self.context = append_to_context(self.context, msg)
        self._undo_stack.clear()
        self._batch = 0
        self._invalidate()
        return msg

    def set_context(self, context: str) -> None:
        """设置/更新场景（例如粘贴对方刚发来的消息）。"""
        self.context = context.strip()
        self._batch = 0
        self._invalidate()
