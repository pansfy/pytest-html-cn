"""
@File    : test_calc.py
@Time    : 2021.5.11 16:52
@Author  : Mr.潘
@Contact : pansf <pansfy@163.com>
@Version : 1.0
@License : Apache License Version 2.0
@Desc    : None
"""
import pytest


def calc(a, b):
    """简易计算."""
    if isinstance(a, int) and isinstance(b, int):
        return a + b
    else:
        raise ValueError("非数值不能进行计算.")


class TestCaseCalc:
    def test_calc_01(self):
        """测试点1:有效数值计算."""
        assert calc(10, 20) == 30

    def test_calc_02(self):
        """测试点2:无效数值计算."""
        with pytest.raises(ValueError):
            calc(10, "abc")

    def test_calc_03(self):
        """测试点3:失败的用例."""
        assert calc(10, 50) == 50

    @pytest.mark.skip("暂不执行")
    def test_calc_04(self):
        """测试点4:跳过的用例."""
        assert calc(100, 200) == 300

    @pytest.mark.xfail
    def test_calc_05(self):
        """测试点5:预期的失败."""
        assert calc(100, 200) == 400

    @pytest.mark.xfail
    def test_calc_06(self):
        """测试点6:未知的通过."""
        assert calc(100, 200) == 300
