"""题目规范化与哈希键：缓存命中率的根基（纯函数）。

规范（design §4）：
- 切分：按分隔符优先级（### > ## > \\n > | > ; > ； > \\t），取非空片段最多者
- 清洗：剥离选项字母前缀（A. / A、 / (A) / A） / A: 等）
- 归一（哈希与匹配共用）：全角转半角（NFKC）→ 删空白与零宽 → casefold → 去尾部标点
- 哈希键：md5(norm(title) + "|" + qtype + "|" + 排序后选项 \x1f 拼接)，
  judgement/completion 不把选项纳入键（判断题选项标签不稳定）
"""

import hashlib
import re
import unicodedata

VALID_QTYPES = ("single", "multiple", "judgement", "completion")
NO_OPTION_QTYPES = ("judgement", "completion")

# 顺序即优先级
SEPARATORS: tuple[str, ...] = ("###", "##", "\n", "|", ";", "；", "\t")

# 分隔符本身不是内容（如仅由 # 组成的碎片）
JUNK_FRAGMENT_RE = re.compile(r"^#+$")

# 零宽字符：ZWSP/ZWNJ/ZWJ/WJ/UTF-8 BOM
ZERO_WIDTH_RE = re.compile(r"[​-‍⁠﻿]")
WHITESPACE_RE = re.compile(r"\s+")
TRAILING_PUNCT_RE = re.compile(r"[。，、,．.!！?？;；:：…~～\"'“”‘’]+$")

# 字母前缀：可选 ( 或 （ + 单个字母 + 分隔符（. 、 : ： ) ））
OPTION_PREFIX_RE = re.compile(r"^[\(（]?[A-Za-z][\.\、\:\：\)）]\s*")


def normalize_text(text: str) -> str:
    """归一化：NFKC → 删空白/零宽 → casefold → 去尾部标点。"""
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH_RE.sub("", text)
    text = WHITESPACE_RE.sub("", text)
    text = text.casefold()
    return TRAILING_PUNCT_RE.sub("", text)


def strip_option_prefix(option: str) -> str:
    """剥离选项的字母前缀（A. / A、 / (A) / A） / A: 等），Apple 这类普通词不受影响。"""
    return OPTION_PREFIX_RE.sub("", option.strip(), count=1).strip()


def option_key(option: str) -> str:
    """选项匹配键：先 NFKC 归一（全角前缀折叠为 ASCII）再剥字母前缀，再归一尾标点。"""
    normalized = normalize_text(option)
    stripped = OPTION_PREFIX_RE.sub("", normalized, count=1)
    return normalize_text(stripped)


def split_options(raw: str | None) -> list[str]:
    """按分隔符优先级切分，取切出非空片段最多的结果；全空视为无选项。"""
    if not raw or not raw.strip():
        return []
    best: list[str] = []
    for separator in SEPARATORS:
        fragments = [part.strip() for part in raw.split(separator)]
        fragments = [part for part in fragments if part and not JUNK_FRAGMENT_RE.fullmatch(part)]
        if len(fragments) > len(best):
            best = fragments
    return best


def build_cache_key(title: str, qtype: str, options: list[str]) -> str:
    """MD5 hex 主键：选项排序参与（乱序重放同键），判断/填空不含选项。"""
    if qtype in NO_OPTION_QTYPES:
        option_part = ""
    else:
        option_part = "\x1f".join(sorted(option_key(o) for o in options))
    payload = f"{normalize_text(title)}|{qtype}|{option_part}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def canonical_qtype(raw: str | None) -> str:
    """题型归一：非法/缺失回退 single。"""
    if raw is None:
        return "single"
    qtype = raw.strip().casefold()
    return qtype if qtype in VALID_QTYPES else "single"
