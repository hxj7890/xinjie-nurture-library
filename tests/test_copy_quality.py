import unittest

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


if __name__ == "__main__":
    unittest.main()
