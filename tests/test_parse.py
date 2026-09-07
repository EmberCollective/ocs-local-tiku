"""LLM 输出解析与内容→字母映射（纯函数，防错误答案固化的关键）。"""

import pytest

from app.answer.parse import ParseError, content_to_response, parse_llm_reply

OPTIONS = ["A. 北京", "B. 上海", "C. 广州", "D. 深圳"]


class TestParseLlmReplyWrappers:
    def test_strips_markdown_code_fence(self):
        assert parse_llm_reply("```json\n北京\n```", "single", OPTIONS) == "A. 北京"

    def test_strips_answer_prefix(self):
        assert parse_llm_reply("答案：北京", "single", OPTIONS) == "A. 北京"

    def test_strips_prefix_and_fence_together(self):
        assert parse_llm_reply("答案：```\n北京\n```", "single", OPTIONS) == "A. 北京"

    def test_strips_whitespace(self):
        assert parse_llm_reply("  \n北京 \n", "single", OPTIONS) == "A. 北京"


class TestParseLlmReplyChoice:
    def test_single_option_text_matched(self):
        assert parse_llm_reply("北京", "single", OPTIONS) == "A. 北京"

    def test_single_pure_letter_translated_to_option_text(self):
        assert parse_llm_reply("A", "single", OPTIONS) == "A. 北京"
        assert parse_llm_reply("b", "single", OPTIONS) == "B. 上海"

    def test_option_text_with_prefix_matched(self):
        assert parse_llm_reply("A. 北京", "single", OPTIONS) == "A. 北京"

    def test_multiple_joined_by_hash_in_option_order(self):
        assert parse_llm_reply("深圳#北京", "multiple", OPTIONS) == "A. 北京#D. 深圳"

    def test_multiple_separators_normalized(self):
        assert parse_llm_reply("北京，上海、广州", "multiple", OPTIONS) == "A. 北京#B. 上海#C. 广州"

    def test_partial_match_keeps_raw_tail(self):
        # 北京可匹配，南京不能 → 保留原文
        assert parse_llm_reply("北京#南京", "multiple", OPTIONS) == "A. 北京#南京"

    def test_single_no_match_keeps_raw(self):
        # 未匹配保留原文，交给 OCS 文本相似度兜底（design §12）
        assert parse_llm_reply("天津", "single", OPTIONS) == "天津"

    def test_match_is_format_insensitive(self):
        # 全角/空白/大小写/前缀差异不影响匹配
        assert parse_llm_reply("ｂ． 上海", "single", OPTIONS) == "B. 上海"


class TestParseLlmReplyJudgement:
    @pytest.mark.parametrize("alias", ["对", "正确", "√", "T", "true", "True", "yes", "是"])
    def test_true_aliases(self, alias):
        assert parse_llm_reply(alias, "judgement", []) == "正确"

    @pytest.mark.parametrize("alias", ["错", "错误", "×", "F", "false", "False", "no", "否"])
    def test_false_aliases(self, alias):
        assert parse_llm_reply(alias, "judgement", []) == "错误"

    def test_unrecognized_judgement_raises(self):
        with pytest.raises(ParseError):
            parse_llm_reply("地球是平的这件事不好说", "judgement", [])


class TestParseLlmReplyCompletion:
    def test_plain_text_passthrough(self):
        assert parse_llm_reply("红楼梦", "completion", []) == "红楼梦"

    def test_completion_with_prefix_stripped(self):
        assert parse_llm_reply("答案：红楼梦", "completion", []) == "红楼梦"


class TestParseLlmReplyFailures:
    def test_empty_raises(self):
        with pytest.raises(ParseError):
            parse_llm_reply("", "single", OPTIONS)

    def test_whitespace_only_raises(self):
        with pytest.raises(ParseError):
            parse_llm_reply("  \n\t ", "single", OPTIONS)

    def test_fence_only_raises(self):
        with pytest.raises(ParseError):
            parse_llm_reply("```\n\n```", "single", OPTIONS)

    @pytest.mark.parametrize("refusal", ["抱歉，我无法回答这个问题", "我不能提供答案", "sorry, I cannot help"])
    def test_refusal_raises(self, refusal):
        with pytest.raises(ParseError):
            parse_llm_reply(refusal, "single", OPTIONS)


class TestContentToResponse:
    def test_single_content_to_letter(self):
        assert content_to_response("A. 北京", "single", OPTIONS) == "A"

    def test_multiple_letters_in_current_option_order(self):
        assert content_to_response("D. 深圳#A. 北京", "multiple", OPTIONS) == "A#D"

    def test_reordered_options_letters_follow_current_order(self):
        # 缓存内容按首见选项顺序存储；重放时选项乱序 → 字母按当前顺序映射
        reordered = ["D. 深圳", "B. 上海", "C. 广州", "A. 北京"]
        assert content_to_response("A. 北京#D. 深圳", "multiple", reordered) == "A#D"
        assert content_to_response("B. 上海", "single", reordered) == "B"

    def test_unmatched_tail_kept_raw(self):
        assert content_to_response("A. 北京#南京", "multiple", OPTIONS) == "A#南京"

    def test_judgement_passthrough(self):
        assert content_to_response("正确", "judgement", []) == "正确"
        assert content_to_response("错误", "judgement", []) == "错误"

    def test_completion_passthrough(self):
        assert content_to_response("红楼梦", "completion", []) == "红楼梦"

    def test_content_without_prefix_still_matches(self):
        assert content_to_response("北京", "single", OPTIONS) == "A"

    def test_pure_letter_content_translated(self):
        # 容错：缓存内容本身是字母（异常数据）时也能映射
        assert content_to_response("A", "single", OPTIONS) == "A"
