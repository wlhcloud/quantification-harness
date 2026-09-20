"""字典 transform 表达式测试（修复：正则转义丢失导致 scale 分支不可达）。

修复前：`_BOOL_EQ = re.compile(r"^vs*===s*'1's*||s*vs*===s*1$")` 含有空分支（`||`），
对任意字符串都匹配成功 → `v*100` / `v/100` 这类缩放表达式全部被当成布尔等式返回 True/False，
末尾 `return None` 分支永不触达。当时线上 410 条映射恰好只用 `v==='1'||v===1`，所以未造成
数据污染，但任何新增缩放映射都会静默写错值。
"""
from __future__ import annotations

import unittest

from quant_sync import upstream


class TransformRegexTest(unittest.TestCase):
    def test_bool_regex_does_not_match_everything(self):
        # 核心回归：布尔正则必须只匹配布尔等式，不能匹配 scale 表达式
        self.assertFalse(upstream._BOOL_EQ.match("v*100"))
        self.assertFalse(upstream._BOOL_EQ.match("v/100"))
        self.assertFalse(upstream._BOOL_EQ.match("value*10"))
        self.assertIsNotNone(upstream._BOOL_EQ.match("v==='1'||v===1"))

    def test_scale_regex(self):
        self.assertIsNotNone(upstream._SCALE.match("v*100"))
        self.assertIsNotNone(upstream._SCALE.match("v/100"))
        self.assertIsNotNone(upstream._SCALE.match("value*10"))
        self.assertIsNotNone(upstream._SCALE.match("value / 1000"))
        self.assertIsNone(upstream._SCALE.match("v*abc"))


class ApplyTransformTest(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(upstream.apply_transform(None, 5), 5)
        self.assertEqual(upstream.apply_transform("", 5), 5)
        self.assertEqual(upstream.apply_transform("v", 5), 5)
        self.assertIsNone(upstream.apply_transform("v", None))

    def test_boolean_equality(self):
        expr = "v==='1'||v===1"
        self.assertIs(upstream.apply_transform(expr, "1"), True)
        self.assertIs(upstream.apply_transform(expr, 1), True)
        self.assertIs(upstream.apply_transform(expr, "0"), False)
        self.assertIs(upstream.apply_transform(expr, 2), False)

    def test_numeric_scale(self):
        self.assertEqual(upstream.apply_transform("v*100", 2), 200.0)
        self.assertEqual(upstream.apply_transform("v/100", 250), 2.5)
        self.assertEqual(upstream.apply_transform("value*10", 1), 10.0)
        self.assertEqual(upstream.apply_transform("value / 1000", 5000), 5.0)
        # 非数值输入按契约返回 None（而不是被布尔分支吞成 False）
        self.assertIsNone(upstream.apply_transform("v*100", "abc"))
        self.assertIsNone(upstream.apply_transform("v*100", None))

    def test_unknown_expression_returns_none(self):
        self.assertIsNone(upstream.apply_transform("round(v, 2)", 1.234))
        self.assertIsNone(upstream.apply_transform("v+1", 1))


if __name__ == "__main__":
    unittest.main()
