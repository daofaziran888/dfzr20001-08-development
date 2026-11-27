"""复烘处置条业务测试：干燥超标 → 复烘状态流转 → 值班员实测 → 分级领取全链路。

场景对应：干燥确认标含水率警告后登记复烘处置条，值班员取同盘中心和边缘
重新实测（均 5.8%，低于 6% 交接上限）并确认可交接，分级工位再领取产出。
"""
from __future__ import annotations

import unittest

from app.services import service_for


class RedryingCase(unittest.TestCase):
    """公共工具：造一盘初烘超标的干燥记录和它名下的复烘处置条。"""

    @classmethod
    def setUpClass(cls):
        cls.drying_svc = service_for("drying")
        cls.redrying_svc = service_for("redrying")
        cls.grading_svc = service_for("grading")

    def _confirmed_drying(self, moisture: str = "8.4") -> dict:
        entry, errors = self.drying_svc.create_entry({
            "起始温度": "60", "终止温度": "90", "干燥时长": "4小时", "含水率": moisture,
        })
        self.assertIsNotNone(entry, errors)
        entry, msg = self.drying_svc.run_action(int(entry["id"]), "记录干燥")
        self.assertIsNotNone(entry, msg)
        return entry

    def _new_disposition(self, drying_id: int, tray: str, initial: str = "8.4",
                         limit: str = "6") -> dict:
        entry, errors = self.redrying_svc.create_entry({
            "盘号": tray, "干燥记录ID": drying_id, "初烘含水率": initial,
            "交接上限": limit, "值班员": "值班员甲",
        })
        self.assertIsNotNone(entry, errors)
        return entry

    def _new_grading(self, drying_id: int, debris: str = "3"):
        return self.grading_svc.create_entry({
            "筛号规格": "6目", "碎末比例": debris, "等级判定": "一级",
            "评审人员": "品控乙", "上游工序": "drying", "上游记录ID": drying_id,
        })


class TestDispositionRegistration(RedryingCase):
    """登记约束：干燥记录必须存在；同一盘货只挂一个有效复烘处置条。"""

    def test_create_requires_existing_drying(self):
        entry, errors = self.redrying_svc.create_entry({
            "盘号": "TRAY-X0", "干燥记录ID": 999999, "初烘含水率": "8.4",
            "交接上限": "6", "值班员": "值班员甲",
        })
        self.assertIsNone(entry)
        self.assertTrue(any("不存在" in e for e in errors))

    def test_one_active_disposition_per_tray(self):
        drying = self._confirmed_drying()
        first = self._new_disposition(int(drying["id"]), "TRAY-U1")
        self.assertEqual(first["status"], "草稿")

        # 同一盘再挂第二张有效处置条 → 拒绝
        dup, errors = self.redrying_svc.create_entry({
            "盘号": "TRAY-U1", "干燥记录ID": int(drying["id"]), "初烘含水率": "8.4",
            "交接上限": "6", "值班员": "值班员乙",
        })
        self.assertIsNone(dup)
        self.assertTrue(any("只挂一个有效复烘处置条" in e for e in errors))

        # 原处置条终止后不再是有效条，可重新登记
        done, msg = self.redrying_svc.run_action(int(first["id"]), "终止复烘")
        self.assertIsNotNone(done, msg)
        again, errors = self.redrying_svc.create_entry({
            "盘号": "TRAY-U1", "干燥记录ID": int(drying["id"]), "初烘含水率": "8.4",
            "交接上限": "6", "值班员": "值班员乙",
        })
        self.assertIsNotNone(again, errors)


class TestRedryingFullScenario(RedryingCase):
    """主场景：正式启动复烘 → 质检补报 → 值班员实测 → 确认交接 → 分级领取。"""

    def test_full_flow(self):
        # 干燥确认：初烘含水率 8.4% 超标，确认时标含水率警告
        drying = self._confirmed_drying("8.4")
        self.assertEqual(drying.get("moisture_warning"), "含水率超标，建议复烘")
        drying_id = int(drying["id"])

        # 新增复烘处置条并正式启动（烘到一半）
        dispo = self._new_disposition(drying_id, "TRAY-F1")
        dispo_id = int(dispo["id"])
        dispo, msg = self.redrying_svc.run_action(dispo_id, "启动复烘")
        self.assertIsNotNone(dispo, msg)
        self.assertEqual(dispo["status"], "复烘中")

        # 质检补报初烘样品的正确含水率已达标：另存，不改写初烘原始记录
        dispo, msg = self.redrying_svc.run_action(dispo_id, "补报初烘",
                                                  {"质检补报含水率": "5.6"})
        self.assertIsNotNone(dispo, msg)
        self.assertEqual(dispo.get("质检补报含水率"), "5.6")
        self.assertEqual(dispo.get("初烘含水率"), "8.4")  # 旧的初烘超标按该盘实际接受
        self.assertEqual(dispo["status"], "复烘中")

        # 干燥班尚未向分级发出交接：分级工位此时尝试领这盘 → 拒绝
        grading, errors = self._new_grading(drying_id)
        self.assertIsNone(grading)
        self.assertTrue(any("确认可交接" in e for e in errors))

        # 值班员取同一盘中心和边缘重新实测，均 5.8%，秤上重量记好
        dispo, msg = self.redrying_svc.run_action(
            dispo_id, "提交实测",
            {"中心实测": "5.8", "边缘实测": "5.8", "复烘实测重量": "42.5"})
        self.assertIsNotNone(dispo, msg)
        self.assertEqual(dispo.get("复烘实测含水率"), "5.8")  # 复烘后实测按该盘实际接受
        self.assertEqual(dispo.get("复烘实测重量"), "42.5")
        self.assertEqual(dispo.get("初烘含水率"), "8.4")  # 两者各按实际保留

        # 值班员按本盘实测确认可交接（5.8% ≤ 6% 交接上限）
        dispo, msg = self.redrying_svc.run_action(dispo_id, "确认交接")
        self.assertIsNotNone(dispo, msg)
        self.assertEqual(dispo["status"], "可交接")
        self.assertEqual(dispo.get("发料决定"), "可发料")

        # 分级工位领这盘：创建并确认分级 → 实际领取
        grading, errors = self._new_grading(drying_id)
        self.assertIsNotNone(grading, errors)
        grading, msg = self.grading_svc.run_action(int(grading["id"]), "确认分级")
        self.assertIsNotNone(grading, msg)

        # 处置条已实际领取，且只挂这一次
        dispo = self.redrying_svc.get_entry(dispo_id)
        self.assertEqual(dispo["status"], "已领取")
        self.assertEqual(str(dispo.get("领取分级记录ID")), str(grading["id"]))

        # 分级记录保留当时的含水率依据；重量按复烘实测，不叠加初烘的重量
        self.assertEqual(grading.get("含水率依据"), "5.8")
        self.assertEqual(str(grading.get("上游重量")), "42.5")
        self.assertAlmostEqual(float(grading.get("产出重量")), round(42.5 * 0.97, 3))

        # 同一时段另一张分级单再尝试领这盘 → 最多实际领取一次
        second, errors = self._new_grading(drying_id)
        self.assertIsNone(second)
        self.assertTrue(any("最多实际领取一次" in e for e in errors))

        # 重交复烘结果：只恢复本处置条，不覆写另一轮盖好的签收
        dispo, msg = self.redrying_svc.run_action(
            dispo_id, "提交实测", {"中心实测": "6.3", "边缘实测": "6.3"})
        self.assertIsNotNone(dispo, msg)
        self.assertEqual(dispo.get("复烘实测含水率"), "6.3")  # 处置条本身恢复/更新
        grading_after = self.grading_svc.get_entry(int(grading["id"]))
        self.assertEqual(grading_after.get("含水率依据"), "5.8")  # 签收依据不被覆写

        # 班组回看：炉上办理状态与分级实际收料
        review = self.redrying_svc.tray_review(drying_id)
        self.assertTrue(review["已实际领取"])
        self.assertTrue(review["分级实际收料"])
        self.assertEqual(review["领取处置条ID"], dispo_id)


class TestHandoverGate(RedryingCase):
    """确认交接必须由值班员按本盘实测判定，且不超过该盘交接上限。"""

    def test_confirm_requires_measurement(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-G1")
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "启动复烘")
        self.assertIsNotNone(entry, msg)
        # 未提交实测 → 拒绝
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "确认交接")
        self.assertIsNone(entry)
        self.assertIn("实测", msg)

    def test_confirm_rejects_over_limit(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-G2")
        self.redrying_svc.run_action(int(dispo["id"]), "启动复烘")
        self.redrying_svc.run_action(int(dispo["id"]), "提交实测",
                                     {"中心实测": "6.5", "边缘实测": "6.2"})
        # 6.5% 高于 6% 交接上限 → 拒绝
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "确认交接")
        self.assertIsNone(entry)
        self.assertIn("交接上限", msg)
        # 分级工位依旧领不到
        grading, errors = self._new_grading(int(drying["id"]))
        self.assertIsNone(grading)

    def test_confirm_accepts_measurement_with_action(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-G3")
        self.redrying_svc.run_action(int(dispo["id"]), "启动复烘")
        # 当次实测随确认动作一并提交
        entry, msg = self.redrying_svc.run_action(
            int(dispo["id"]), "确认交接",
            {"中心实测": "5.8", "边缘实测": "5.7", "复烘实测重量": "40"})
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry["status"], "可交接")
        self.assertEqual(entry.get("复烘实测含水率"), "5.8")  # 取中心/边缘就高值


class TestTerminationRules(RedryingCase):
    """终止复烘：复烘实物已影响原含水率后，须凭当前盘实际测量决定发料。"""

    def test_terminate_draft_without_measurement(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-T1")
        # 未启动复烘（实物未影响含水率）→ 可直接终止，禁止发料
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "终止复烘")
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry["status"], "已终止")
        self.assertEqual(entry.get("发料决定"), "禁止发料")

    def test_terminate_started_requires_measurement(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-T2")
        self.redrying_svc.run_action(int(dispo["id"]), "启动复烘")
        # 已正式启动但未提交当前盘实测 → 不允许直接终止
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "终止复烘")
        self.assertIsNone(entry)
        self.assertIn("实际测量", msg)

    def test_terminate_decides_issuance_by_measurement(self):
        # 实测达标 → 终止后仍可发料，分级可凭实测领取
        drying_ok = self._confirmed_drying()
        dispo_ok = self._new_disposition(int(drying_ok["id"]), "TRAY-T3")
        self.redrying_svc.run_action(int(dispo_ok["id"]), "启动复烘")
        entry, msg = self.redrying_svc.run_action(
            int(dispo_ok["id"]), "终止复烘",
            {"中心实测": "5.5", "边缘实测": "5.8", "复烘实测重量": "41"})
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry["status"], "已终止")
        self.assertEqual(entry.get("发料决定"), "可发料")

        grading, errors = self._new_grading(int(drying_ok["id"]))
        self.assertIsNotNone(grading, errors)
        grading, msg = self.grading_svc.run_action(int(grading["id"]), "确认分级")
        self.assertIsNotNone(grading, msg)
        self.assertEqual(grading.get("含水率依据"), "5.8")
        self.assertEqual(str(grading.get("上游重量")), "41")

        # 实测超标 → 终止后禁止发料，分级不能领
        drying_bad = self._confirmed_drying()
        dispo_bad = self._new_disposition(int(drying_bad["id"]), "TRAY-T4")
        self.redrying_svc.run_action(int(dispo_bad["id"]), "启动复烘")
        entry, msg = self.redrying_svc.run_action(
            int(dispo_bad["id"]), "终止复烘", {"复烘实测含水率": "6.8"})
        self.assertIsNotNone(entry, msg)
        self.assertEqual(entry.get("发料决定"), "禁止发料")
        grading_bad, errors = self._new_grading(int(drying_bad["id"]))
        self.assertIsNone(grading_bad)

    def test_terminate_after_collected_rejected(self):
        drying = self._confirmed_drying()
        dispo = self._new_disposition(int(drying["id"]), "TRAY-T5")
        self.redrying_svc.run_action(int(dispo["id"]), "启动复烘")
        self.redrying_svc.run_action(int(dispo["id"]), "提交实测",
                                     {"复烘实测含水率": "5.8", "复烘实测重量": "40"})
        self.redrying_svc.run_action(int(dispo["id"]), "确认交接")
        grading, errors = self._new_grading(int(drying["id"]))
        self.assertIsNotNone(grading, errors)
        self.grading_svc.run_action(int(grading["id"]), "确认分级")
        entry, msg = self.redrying_svc.run_action(int(dispo["id"]), "终止复烘")
        self.assertIsNone(entry)
        self.assertIn("已实际领取", msg)


class TestGradingWithoutDisposition(RedryingCase):
    """回归：未挂复烘处置条的干燥记录，分级流程不受影响。"""

    def test_grading_without_disposition_unchanged(self):
        drying = self._confirmed_drying("5.0")  # 含水率达标，无警告
        self.assertNotEqual(drying.get("moisture_warning"), "含水率超标，建议复烘")
        grading, errors = self._new_grading(int(drying["id"]))
        self.assertIsNotNone(grading, errors)
        grading["上游重量"] = "50"
        grading, msg = self.grading_svc.run_action(int(grading["id"]), "确认分级")
        self.assertIsNotNone(grading, msg)
        self.assertIsNone(grading.get("含水率依据"))
        self.assertAlmostEqual(float(grading.get("产出重量")), round(50 * 0.97, 3))


if __name__ == "__main__":
    unittest.main()
