"""精制分级领域服务：按筛号规格分选、碎末比例控制、等级判定，校验上游干燥。"""
from __future__ import annotations
from typing import Any
from app.services.base import BaseModuleService
from app.store import store

class GradingService(BaseModuleService):
    MODULE = "grading"
    ENTITY = "分级结果"
    OPERATION_TERM = "确认分级"
    LIST_FIELDS = ["筛号规格", "碎末比例", "等级判定", "评审人员", "登记编号", "负责人", "生效版本", "备注说明"]
    REQUIRED_FIELDS = ["筛号规格", "碎末比例", "等级判定", "评审人员"]
    STATUS_ORDER = ["草稿", "待确认", "已确认", "已撤回"]
    ACTION_RULES = {"确认分级": "已确认", "提交确认": "待确认", "撤回记录": "已撤回"}
    NEGATIVE_ACTIONS = []

    VALID_GRADES = ["特级", "一级", "二级", "三级", "四级", "等外"]

    def validate_entry(self, values: dict[str, Any]) -> list[str]:
        errors = []
        grade = values.get("等级判定", "")
        if grade not in self.VALID_GRADES:
            errors.append(f"等级判定须为{', '.join(self.VALID_GRADES)}之一")
        debris = store.parse_weight(values.get("碎末比例", "100"))
        if debris < 0 or debris > 100:
            errors.append("碎末比例格式无效")
        # 上游工序校验
        upstream_module = values.get("上游工序")
        upstream_id = values.get("上游记录ID")
        if upstream_module and upstream_id is not None:
            upstream = store.find(upstream_module, int(upstream_id))
            if upstream is None:
                errors.append(f"上游记录 {upstream_module}/{upstream_id} 不存在")
            elif upstream.get("status") != "已确认":
                errors.append(f"上游记录 {upstream_module}/{upstream_id} 未确认，不能进行分级")
        # 复烘处置条核对：未经值班员按本盘实测确认可交接的出料不能送进精制分级
        if upstream_module == "drying" and upstream_id is not None:
            errors.extend(self._check_redrying_gate(int(upstream_id)))
        return errors

    def _redrying(self):
        from app.services import service_for
        return service_for("redrying")

    def _check_redrying_gate(self, drying_id: int) -> list[str]:
        """该盘挂有复烘处置条时，核对产出是不是已经可以领取。"""
        svc = self._redrying()
        if svc is None:
            return []
        dispositions = svc.find_for_tray(drying_id)
        if not dispositions:
            return []
        if svc.tray_collected(drying_id) is not None:
            return ["该盘复烘产出已实际领取，同一盘复烘完成最多实际领取一次"]
        active = svc.active_for_tray(drying_id)
        if active is not None:
            if active.get("status") != "可交接":
                return [f"复烘处置条#{active['id']}状态为「{active.get('status')}」，"
                        f"未经值班员按本盘实测确认可交接，不能送进精制分级"]
            return []
        if any(svc.issuable(d) for d in dispositions):
            return []
        return ["该盘复烘处置已终止且无实测发料依据，不能送进精制分级"]

    def auto_grade(self, debris_rate: float, sieve: str) -> str:
        """根据碎末率和筛号规格自动判定等级。"""
        if debris_rate <= 2:
            return "特级"
        elif debris_rate <= 5:
            return "一级"
        elif debris_rate <= 10:
            return "二级"
        elif debris_rate <= 20:
            return "三级"
        else:
            return "等外"

    def on_confirm(self, entry: dict[str, Any], action: str) -> tuple[bool, str | None]:
        """分级确认后更新批次产出重量；挂复烘处置条的盘先完成实际领取。"""
        if action == "确认分级":
            ok, msg = self._collect_redrying_output(entry)
            if not ok:
                return False, msg
            debris = store.parse_weight(entry.get("碎末比例", "0"))
            input_weight = store.parse_weight(entry.get("上游重量", "0"))
            if input_weight > 0:
                entry["产出重量"] = round(input_weight * (1.0 - debris / 100.0), 3)
        return True, None

    def _collect_redrying_output(self, entry: dict[str, Any]) -> tuple[bool, str | None]:
        """确认分级时实际领取复烘产出：最多一次、不叠加初烘重量、留存含水率依据。"""
        upstream_module = entry.get("上游工序")
        upstream_id = entry.get("上游记录ID")
        if upstream_module != "drying" or upstream_id is None:
            return True, None
        svc = self._redrying()
        if svc is None:
            return True, None
        dispositions = svc.find_for_tray(int(upstream_id))
        if not dispositions:
            return True, None
        collected = svc.tray_collected(int(upstream_id))
        if collected is not None:
            if str(collected.get("领取分级记录ID")) == str(entry.get("id")):
                return True, None  # 本记录已领取，幂等放行
            return False, "该盘复烘产出已实际领取，同一盘复烘完成最多实际领取一次"
        target = svc.active_for_tray(int(upstream_id))
        if target is None:
            target = next((d for d in dispositions if svc.issuable(d)), None)
        if target is None or not svc.issuable(target):
            status = target.get("status") if target is not None else "无有效处置条"
            return False, f"复烘处置条未经值班员按本盘实测确认可交接（{status}），不能确认分级"
        result, msg = svc.collect(int(target["id"]), grading_id=entry.get("id"))
        if result is None:
            return False, msg
        # 分级记录保留领取时的含水率依据；投入重量按复烘实测，不叠加初烘的重量
        entry["含水率依据"] = target.get("复烘实测含水率")
        if store.parse_weight(target.get("复烘实测重量", "0")) > 0:
            entry["上游重量"] = target.get("复烘实测重量")
        return True, None

    def get_gradings_for_blending(self, batch_no: str) -> list[dict[str, Any]]:
        """获取指定批次可用于拼配的分级结果。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("批次号") == batch_no and r.get("status") == "已确认"]

    def get_by_grade(self, grade: str) -> list[dict[str, Any]]:
        """按等级获取分级结果。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("等级判定") == grade and r.get("status") == "已确认"]