"""规范化纯函数全覆盖：切分、前缀剥离、归一、哈希键（缓存命中率根基）。"""

import hashlib

from app.normalize import (
    build_cache_key,
    canonical_qtype,
    normalize_text,
    split_options,
    strip_option_prefix,
)


class TestSplitOptions:
    def test_newline_split(self):
        assert split_options("A. 甲\nB. 乙\nC. 丙") == ["A. 甲", "B. 乙", "C. 丙"]

    def test_double_hash_split(self):
        assert split_options("甲##乙##丙") == ["甲", "乙", "丙"]

    def test_triple_hash_beats_double_hash_on_priority(self):
        # "###" 与 "##" 切出非空片段数相同（3），优先级高者胜 → 片段更干净
        assert split_options("甲###乙###丙") == ["甲", "乙", "丙"]

    def test_pipe_and_chinese_semicolon_split(self):
        assert split_options("甲|乙|丙") == ["甲", "乙", "丙"]
        assert split_options("甲；乙；丙") == ["甲", "乙", "丙"]

    def test_most_fragments_wins_regardless_of_priority(self):
        # 换行切出 3 片段 > ## 的 2 片段
        assert split_options("甲##乙\n丙\n丁") == ["甲##乙", "丙", "丁"]

    def test_fragments_are_stripped(self):
        assert split_options("  甲 \n 乙 \n\t丙\t") == ["甲", "乙", "丙"]

    def test_blank_input_returns_empty(self):
        assert split_options(None) == []
        assert split_options("") == []
        assert split_options("   \n  ") == []

    def test_separator_only_input_returns_empty(self):
        assert split_options("###") == []

    def test_no_separator_single_option(self):
        assert split_options("唯一的选项") == ["唯一的选项"]


class TestStripOptionPrefix:
    def test_dot_prefix(self):
        assert strip_option_prefix("A. 选项") == "选项"
        assert strip_option_prefix("A.选项") == "选项"

    def test_chinese_pause_mark_prefix(self):
        assert strip_option_prefix("B、选项") == "选项"

    def test_parenthesized_prefix(self):
        assert strip_option_prefix("(C) 选项") == "选项"
        assert strip_option_prefix("（D）选项") == "选项"

    def test_full_width_bracket_suffix_prefix(self):
        assert strip_option_prefix("E）选项") == "选项"

    def test_colon_prefix(self):
        assert strip_option_prefix("F:选项") == "选项"
        assert strip_option_prefix("G：选项") == "选项"

    def test_lowercase_prefix(self):
        assert strip_option_prefix("b. 选项") == "选项"

    def test_plain_word_not_stripped(self):
        assert strip_option_prefix("Apple pie") == "Apple pie"

    def test_no_prefix_unchanged(self):
        assert strip_option_prefix("选项") == "选项"


class TestNormalizeText:
    def test_fullwidth_to_halfwidth(self):
        assert normalize_text("（ＡＢ）") == normalize_text("(AB)") == "(ab)"

    def test_casefold(self):
        # 设计规定删除所有空白：大小写折叠 + 空白删除后应相等
        assert normalize_text("Hello WORLD") == "helloworld"

    def test_whitespace_removed(self):
        assert normalize_text("a b\tc\nd") == "abcd"

    def test_zero_width_removed(self):
        assert normalize_text("a​b⁠c﻿d") == "abcd"

    def test_trailing_punctuation_removed(self):
        assert normalize_text("题目。") == "题目"
        assert normalize_text("答案!") == "答案"
        assert normalize_text("题目…”") == "题目"

    def test_leading_punctuation_kept(self):
        assert normalize_text("《题目》").startswith("《")


class TestBuildCacheKey:
    def test_returns_32_char_hex(self):
        key = build_cache_key("题目", "single", ["甲", "乙"])
        assert len(key) == 32
        int(key, 16)  # 合法 hex

    def test_option_order_does_not_change_key(self):
        a = build_cache_key("题目", "multiple", ["A. 甲", "B. 乙", "C. 丙"])
        b = build_cache_key("题目", "multiple", ["C. 丙", "A. 甲", "B. 乙"])
        assert a == b

    def test_option_prefix_and_format_do_not_change_key(self):
        a = build_cache_key("题目", "single", ["A. 甲"])
        b = build_cache_key("题目", "single", ["甲"])
        assert a == b

    def test_judgement_key_ignores_options(self):
        a = build_cache_key("题目", "judgement", [])
        b = build_cache_key("题目", "judgement", ["对", "错"])
        assert a == b

    def test_completion_key_ignores_options(self):
        a = build_cache_key("题目", "completion", [])
        b = build_cache_key("题目", "completion", ["无关选项"])
        assert a == b

    def test_different_titles_differ(self):
        assert build_cache_key("题目一", "single", ["甲"]) != build_cache_key("题目二", "single", ["甲"])

    def test_different_types_differ(self):
        assert build_cache_key("题目", "single", ["甲"]) != build_cache_key("题目", "multiple", ["甲"])

    def test_title_normalization_applied(self):
        a = build_cache_key("  题 目。", "single", ["甲"])
        b = build_cache_key("题目", "single", ["甲"])
        assert a == b

    def test_format_locked_to_md5_of_pipe_joined_payload(self):
        payload = "题目|single|apple\x1fbanana"
        assert build_cache_key("题目", "single", ["B. banana", "A. apple"]) == hashlib.md5(payload.encode("utf-8")).hexdigest()


class TestCanonicalQtype:
    def test_valid_types_passthrough(self):
        assert canonical_qtype("single") == "single"
        assert canonical_qtype("multiple") == "multiple"
        assert canonical_qtype("judgement") == "judgement"
        assert canonical_qtype("completion") == "completion"

    def test_invalid_falls_back_to_single(self):
        assert canonical_qtype("essay") == "single"
        assert canonical_qtype("单选题") == "single"

    def test_none_and_blank_fall_back_to_single(self):
        assert canonical_qtype(None) == "single"
        assert canonical_qtype("") == "single"
