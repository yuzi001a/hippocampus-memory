"""Tool Log Offloader — 长 session 大工具输出外置

设计目的
--------
长 session (50+ 轮) 中工具输出（搜索结果、代码执行等）会膨胀上下文。
Hermes 的 on_pre_compress hook 在上下文接近限制时被调用, 此时把大工具
输出外置到 refs/{session_id}/{turn_index}.txt, 原文位置用 500 字摘要 +
引用指针替代, 可以显著降低压缩后保留消息的 token 体积。

主要 API
--------
- ToolLogOffloader.offload_tool_logs(messages, session_id, refs_root)
    扫描 messages 列表, 把 role=="tool" 且 content > 5KB 的消息摘要化.
    返回 (offloaded_count, saved_chars) 元组, 同时 in-place 修改 messages.

- ToolLogOffloader.get_original(refs_dir, session_id, turn_index)
    静态方法. 读 refs/{session_id}/{turn_index}.txt, 返回完整原文
    或 None (文件不存在).

内部规范
--------
- 文件路径: {refs_root}/{session_id}/{turn_index}.txt
- 编码: utf-8
- 用 pathlib.Path, 不用 os.path
- 摘要: content[:500] + '\n\n<ref:{session_id}/{turn_index}>  (完整原文: refs/{session_id}/{turn_index}.txt)'
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.offload")

# 触发 offload 的最小 content 字节数 (5KB)
THRESHOLD_BYTES = 5 * 1024
# 摘要保留的字符数
SUMMARY_CHARS = 500


class ToolLogOffloader:
    """把大工具输出外置到 refs/{session_id}/{turn_index}.txt"""

    @staticmethod
    def _refs_dir_for(refs_root: str | Path, session_id: str) -> Path:
        """构造 refs/{session_id} 目录, 不存在则创建"""
        refs_dir = Path(refs_root) / "refs" / session_id
        refs_dir.mkdir(parents=True, exist_ok=True)
        return refs_dir

    @staticmethod
    def _extract_content(msg: Any) -> str:
        """从消息中提取 content 字符串, 兼容多种 message 形态"""
        if isinstance(msg, dict):
            content = msg.get("content", "")
        else:
            content = getattr(msg, "content", "")
        return str(content or "")

    @staticmethod
    def _make_summary(full_content: str, session_id: str, turn_index: int) -> str:
        """构造摘要 + 引用指针"""
        summary = full_content[:SUMMARY_CHARS]
        ref_path = f"refs/{session_id}/{turn_index}.txt"
        return (
            f"{summary}\n\n"
            f"<ref:{session_id}/{turn_index}>  "
            f"(完整原文: {ref_path})"
        )

    @classmethod
    def offload_tool_logs(
        cls,
        messages: list,
        session_id: str,
        refs_root: str | Path,
    ) -> tuple[int, int]:
        """扫描 messages, 把 role=="tool" 且 content > 5KB 的消息摘要化

        参数
        ----
        messages: 消息列表 (in-place 修改 — 原列表的 dict 会被改写)
        session_id: 当前 session id (用于构建 refs 路径)
        refs_root: refs 根目录 (通常是 v3-core data root)

        返回
        ----
        (offloaded_count, saved_chars): 卸载的消息数 + 节省的字符数
        """
        if not messages:
            return (0, 0)

        refs_dir = cls._refs_dir_for(refs_root, session_id)
        offloaded = 0
        saved_chars = 0

        for turn_index, msg in enumerate(messages):
            # 1) 角色判定
            if isinstance(msg, dict):
                role = msg.get("role", "")
            else:
                role = getattr(msg, "role", "")
            if role != "tool":
                continue

            # 2) 内容长度判定
            full_content = cls._extract_content(msg)
            content_bytes = len(full_content.encode("utf-8"))
            if content_bytes <= THRESHOLD_BYTES:
                continue

            # 3) 写原文
            ref_file = refs_dir / f"{turn_index}.txt"
            try:
                ref_file.write_text(full_content, encoding="utf-8")
            except OSError as e:
                logger.warning(
                    "offload: 写入 refs 失败 (session=%s, turn=%d): %s",
                    session_id, turn_index, _safe_err(e)[:100],
                )
                continue

            # 4) 替换 content 为摘要
            summary = cls._make_summary(full_content, session_id, turn_index)
            if isinstance(msg, dict):
                msg["content"] = summary
            else:
                # 兼容对象形态 — 直接覆盖属性
                try:
                    msg.content = summary
                except Exception:
                    logger.debug(
                        "offload: 无法修改 msg.content (session=%s, turn=%d, type=%s)",
                        session_id, turn_index, type(msg).__name__,
                    )
                    continue

            saved_chars += content_bytes - len(summary.encode("utf-8"))
            offloaded += 1
            logger.debug(
                "offload: session=%s turn=%d bytes=%d -> summary=%d (refs=%s)",
                session_id, turn_index, content_bytes,
                len(summary.encode("utf-8")), ref_file,
            )

        if offloaded > 0:
            logger.info(
                "offload: session=%s offloaded=%d saved=%d bytes",
                session_id, offloaded, saved_chars,
            )
        return (offloaded, saved_chars)

    @staticmethod
    def get_original(
        refs_dir: str | Path,
        session_id: str,
        turn_index: int,
    ) -> str | None:
        """读 refs/{session_id}/{turn_index}.txt, 返回完整原文或 None"""
        ref_file = Path(refs_dir) / "refs" / session_id / f"{turn_index}.txt"
        if not ref_file.is_file():
            return None
        try:
            return ref_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(
                "get_original: 读 refs 失败 (session=%s, turn=%d): %s",
                session_id, turn_index, _safe_err(e)[:100],
            )
            return None