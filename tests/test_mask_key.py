"""mask_key 打码规则。"""

from app.repository.providers import mask_key


class TestMaskKey:
    def test_masks_middle(self):
        assert mask_key("sk-abc123def456") == "sk-***f456"

    def test_short_key_all_masked_tail(self):
        assert mask_key("s3cr3t") == "***r3t"

    def test_empty_key(self):
        assert mask_key("") == "***"
