"""LLM 输出解析与内容→字母映射（纯函数）。

关键设计（design §4）：缓存与响应共用同一条「内容→字母」映射路径——
parse_llm_reply 把 LLM 原始回复归一成「答案内容」（选项原文 # 拼接 /
正确|错误 / 填空文本）落库；content_to_response 把内容按**当前请求的
选项顺序**映射为字母。多选字母按选项表顺序排列；未匹配片段保留原文
（OCS 端 string-similarity 兜底）。解析失败绝不入缓存（防错误答案固化）。
"""

import re

from app.normalize import NO_OPTION_QTYPES, normalize_text, option_key


class ParseError(Exception):
    """LLM 回复无法解析为可用答案。"""


REFUSAL_MARKERS = (
    "抱歉",
    "无法回答",
    "不能回答",
    "无法确定",
    "我不能",
    "i cannot",
    "i can't",
    "sorry",
    "as an ai",
)
REFUSAL_MAX_LENGTH = 60

TRUE_ALIASES = {"对", "正确", "√", "✓", "t", "true", "yes", "y", "是"}
FALSE_ALIASES = {"错", "错误", "×", "✗", "x", "f", "false", "no", "n", "否"}

# markdown 围栏（```json ... ``` / ``` ... ```）
FENCE_RE = re.compile(r"^```[\w-]*[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)
# 「答案：」类前缀（含【】与加粗变体）
ANSWER_PREFIX_RE = re.compile(r"^(?:【\s*答案\s*】|答案|答)\s*[:：是]?\s*")
# 首尾 markdown 加粗/空白装饰
DECORATION_RE = re.compile(r"^[\*\s]+|[\*\s]+$")
# 多选答案片段分隔：#、换行、|、中英文逗号顿号分号
ANSWER_SPLIT_RE = re.compile(r"[#|\n]+|，|、|;|；|,")
# 片段噪音前缀：列表符号 / 序号
PIECE_NOISE_RE = re.compile(r"^(?:[-*•]\s*|\d+[\.\、\)]\s*)+")

MAX_OPTION_LETTERS = 8


def parse_llm_reply(raw: str, qtype: str, options: list[str]) -> str:
    """LLM 原始回复 → 规范化「答案内容」（入库形态）。失败抛 ParseError。"""
    text = strip_wrappers(raw)
    if not text:
        raise ParseError("LLM 回复为空")
    if is_refusal(text):
        raise ParseError(f"LLM 拒答: {text[:REFUSAL_MAX_LENGTH]}")
    if qtype == "judgement":
        canonical = _canonical_judgement(text)
        if canonical is None:
            raise ParseError(f"判断题答案无法归一: {text[:REFUSAL_MAX_LENGTH]}")
        return canonical
    if qtype == "completion":
        return text
    return _match_options(text, qtype, options)


def content_to_response(content: str, qtype: str, options: list[str]) -> str:
    """「答案内容」→ 响应形态：选项题映射字母（按当前选项顺序），其余原样。"""
    if qtype in NO_OPTION_QTYPES:
        return content
    ordered, unmatched = _match_pieces(content, qtype, options)
    letters = list(dict.fromkeys(chr(ord("A") + options.index(option)) for option in ordered))
    return "#".join(letters + unmatched)


def strip_wrappers(raw: str) -> str:
    """剥离 markdown 围栏、答案前缀、加粗装饰（循环至稳定）。"""
    text = raw.strip()
    for _ in range(3):
        stripped = _strip_once(text)
        if stripped == text:
            break
        text = stripped
    return text.strip()


def is_refusal(text: str) -> bool:
    lowered = text.casefold()
    return len(text) <= REFUSAL_MAX_LENGTH and any(marker in lowered for marker in REFUSAL_MARKERS)


def _strip_once(text: str) -> str:
    fence = FENCE_RE.match(text)
    if fence:
        return fence.group(1)
    without_prefix = ANSWER_PREFIX_RE.sub("", text, count=1)
    return DECORATION_RE.sub("", without_prefix)


def _canonical_judgement(text: str) -> str | None:
    normalized = normalize_text(text)
    if normalized in TRUE_ALIASES:
        return "正确"
    if normalized in FALSE_ALIASES:
        return "错误"
    return None


def _split_pieces(text: str, qtype: str) -> list[str]:
    """单选不切分；多选按分隔符切分（全空则视为整体）。"""
    if qtype == "single":
        return [text]
    pieces = [piece.strip() for piece in ANSWER_SPLIT_RE.split(text)]
    non_empty = [piece for piece in pieces if piece]
    return non_empty or [text.strip()]


def _match_piece(piece: str, options: list[str]) -> list[str]:
    """单个答案片段 → 匹配的选项原文列表（0/1/多个；纯字母按位置展开）。"""
    target = option_key(PIECE_NOISE_RE.sub("", piece.strip()))
    if not target:
        return []
    exact = [option for option in options if option_key(option) == target]
    if exact:
        return exact[:1]
    if target.isalpha() and len(target) <= MAX_OPTION_LETTERS:
        indices = [ord(char) - ord("a") for char in target]
        if all(0 <= index < len(options) for index in indices):
            unique_indices = list(dict.fromkeys(indices))
            return [options[index] for index in unique_indices]
    return []


def _match_pieces(text: str, qtype: str, options: list[str]) -> tuple[list[str], list[str]]:
    """按片段匹配选项：返回（按选项表顺序去重后的匹配选项，未匹配的非空片段）。"""
    matched: list[str] = []
    unmatched: list[str] = []
    for piece in _split_pieces(text, qtype):
        found = _match_piece(piece, options)
        if found:
            matched.extend(option for option in found if option not in matched)
        elif piece.strip():
            unmatched.append(piece.strip())
    ordered = [option for option in options if option in matched]
    return ordered, unmatched


def _match_options(text: str, qtype: str, options: list[str]) -> str:
    """选项题回复 → 内容形态：匹配选项按选项表顺序 # 拼接，未匹配片段保留原文。"""
    ordered, unmatched = _match_pieces(text, qtype, options)
    return "#".join(ordered + unmatched)
