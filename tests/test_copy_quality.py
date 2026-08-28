import unittest
from unittest.mock import patch

from app import main
from app.main import low_quality_content


class CopyQualityTests(unittest.TestCase):
    def test_rejects_invented_performative_metaphor(self):
        content = {
            "title": "洗车路过一下",
            "body": "小电驴沾满泡沫，活脱脱一只白毛小怪兽，谁懂啊。",
            "topics": ["洗车日常", "小电驴"],
        }
        self.assertTrue(low_quality_content(content))

    def test_allows_plain_fact_based_copy(self):
        content = {
            "title": "顺手冲了下车",
            "body": "路过自助洗车点，给小电驴冲了一下。泡沫打得有点多，擦完就走了。",
            "topics": ["洗车日常", "小电驴"],
        }
        self.assertFalse(low_quality_content(content))

    def test_allows_short_neutral_copy_when_facts_are_sparse(self):
        content = {
            "title": "窗边的杯子",
            "body": "桌上有一个白色杯子。",
            "topics": ["桌面", "随手拍"],
        }
        self.assertFalse(low_quality_content(content))

    def test_rejects_comparison_even_without_the_named_example(self):
        content = {
            "title": "泡沫很多",
            "body": "车上的泡沫像云朵一样，今天顺手洗了一下。",
            "topics": ["洗车", "小电驴"],
        }
        self.assertTrue(low_quality_content(content))

    def test_rejects_forced_internet_style_wording(self):
        content = {
            "title": "车洗好了",
            "body": "泡沫直接糊满了车身，拍完就走了。",
            "topics": ["洗车", "小电驴"],
        }
        self.assertTrue(low_quality_content(content))

    def test_reuses_supplied_vision_facts_without_a_second_vision_request(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '{"title":"床架到了","body":"床架先靠墙放着，等床垫到了再收拾。","topics":["居家","卧室"]}'}}]}

        original_key, original_vision_key = main.OPENAI_KEY, main.OPENAI_VISION_KEY
        main.OPENAI_KEY, main.OPENAI_VISION_KEY = "text-key", "vision-key"
        try:
            with patch("app.main.httpx.post", return_value=Response()) as post:
                result = main.image_prompt([], "", facts='{"facts":["银灰色床架靠墙放置"]}')
            self.assertEqual(result["title"], "床架到了")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], f"{main.OPENAI_BASE_URL}/chat/completions")
        finally:
            main.OPENAI_KEY, main.OPENAI_VISION_KEY = original_key, original_vision_key


if __name__ == "__main__":
    unittest.main()
