"""答题 prompt 构造（输出契约与 parse_llm_reply 一一对应）。"""

from app.normalize import NO_OPTION_QTYPES, strip_option_prefix

SYSTEM_PROMPT = """你是一名考试答题助手，根据题目直接给出答案。输出规则：
- 单选题：只输出正确选项的完整文字内容
- 多选题：输出所有正确选项的完整文字内容，多个选项之间用 # 连接
- 判断题：只输出「正确」或「错误」两个词之一
- 填空题：直接输出答案文本
不要输出任何解释、编号、字母代号、引号或多余标点。"""

QTYPE_LABELS = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
}


def build_messages(title: str, qtype: str, options: list[str]) -> list[dict]:
    """构造 chat messages；选项剥前缀后逐行给出，便于回复直接按文本匹配。"""
    lines = [f"题型：{QTYPE_LABELS[qtype]}", f"题目：{title}"]
    if options and qtype not in NO_OPTION_QTYPES:
        lines.append("选项：")
        lines.extend(strip_option_prefix(option) for option in options)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
