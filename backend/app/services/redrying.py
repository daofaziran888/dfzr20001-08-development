"""复烘处置领域服务：复烘处置条的正式状态流转与分级领取门禁。

业务链路：
1. 干燥确认含水率超标 → 登记复烘处置条（同一盘货只挂一个有效处置条）
2. 启动复烘 → 值班员在处置条上提交当次实测（中心/边缘含水率与秤上重量）
3. 值班员按本盘实测确认可交接 → 精制分级工作台凭关联处置条核对后领取产出
4. 同一盘复烘完成最多实际领取一次，重量不叠加初烘，分级记录保留领取时的含水率依据
5. 复烘实物已影响原含水率后，终止复烘也须凭当前盘的实际测量决定发料
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RedryingService(BaseModuleService):
    MODULE = "redrying"
    ENTITY = "复烘处置条"
    OPERATION_TERM = "确认交接"
    LIST_FIELDS = ["盘号", "干燥记录ID", "初烘含水率", "复烘实测含水率", "复烘实测重量",
                   "交接上限", "值班员", "登记编号", "负责人", "生效版本", "备注说明"]
    REQUIRED_FIELDS = ["盘号", "干燥记录ID", "初烘含水率", "交接上限", "值班员"]
    STATUS_ORDER = ["草稿", "复烘中", "可交接", "已领取", "已终止"]
    ACTION_RULES = {
        "启动复烘": "复烘中",
        "提交实测": "复烘中",
        "补报初烘": "复烘中",
        "确认交接": "可交接",
        "领取产出": "已领取",
        "终止复烘": "已终止",
    }
    NEGATIVE_ACTIONS: list[str] = []

    # 有效处置条状态：同一盘货同一时刻只挂一个
    ACTIVE_STATUSES = ("草稿", "复烘中", "可交接")
    # 随动作提交实测时可识别的含水率字段
    MEASURE_KEYS = ("中心实测", "边缘实测", "复烘实测含水率")

    # ── 登记校验 ──

    def validate_entry(self, values: dict[str, Any]) -> list[str]:
        errors = []
        drying_id = values.get("干燥记录ID")
        drying = None
        try:
            drying = store.find("drying", int(drying_id))
        except (TypeError, ValueError):
            drying = None
        if drying is None:
            errors.append(f"干燥记录 {drying_id} 不存在，不能登记复烘处置条")
        else:
            active = self.active_for_tray(int(drying_id))
            if active is not None:
                errors.append(
                    f"同一盘货只挂一个有效复烘处置条：干燥记录 {drying_id} "
                    f"已存在处置条#{active['id']}（{active.get('status')}）")
        if store.parse_weight(values.get("初烘含水率", "0")) <= 0:
            errors.append("必须记录初烘含水率")
        if store.parse_weight(values.get("交接上限", "0")) <= 0:
            errors.append("交接上限必须大于0")
        return errors

    # ── 状态流转（本模块动作语义特殊，整体自管）──

    def run_action(self, entry_id: int, action: str,
                   values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if entry.get("frozen"):
            reason = entry.get("freeze_reason", "未知")
            return None, f"{self.ENTITY} {entry_id} 已被冻结（原因：{reason}），不允许操作"
        values = values or {}
        handlers = {
            "启动复烘": self._start_redrying,
            "提交实测": self._submit_measurement,
            "补报初烘": self._supplement_initial,
            "确认交接": self._confirm_handover,
            "终止复烘": self._terminate,
        }
        if action == "领取产出":
            return self.collect(entry_id, values.get("分级记录ID"))
        handler = handlers.get(action)
        if handler is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        return handler(entry, values)

    def _start_redrying(self, entry: dict[str, Any], values: dict[str, Any]):
        status = entry.get("status")
        if status == "复烘中":
            return entry, f"{self.ENTITY}已处于「复烘中」状态，无需重复操作"
        if status != "草稿":
            return None, f"复烘处置条当前状态为「{status}」，不能启动复烘"
        entry["status"] = "复烘中"
        entry["pending"] = True
        store.log_event(self.MODULE, int(entry["id"]), "启动复烘",
                        {"from": status, "to": "复烘中"})
        return entry, "复烘已正式启动"

    def _submit_measurement(self, entry: dict[str, Any], values: dict[str, Any]):
        status = entry.get("status")
        if status == "已终止":
            return None, "复烘处置条已终止，不再接受实测提交"
        err = self._record_measurement(entry, values)
        if err:
            return None, err
        if status == "已领取":
            # 重交复烘结果：只恢复本处置条，不覆写另一轮盖好的分级签收
            store.log_event(self.MODULE, int(entry["id"]), "重交实测",
                            {"复烘实测含水率": entry.get("复烘实测含水率")})
            return entry, "复烘结果已重交：仅恢复本处置条实测，已实际领取的分级记录保留当时含水率依据"
        store.log_event(self.MODULE, int(entry["id"]), "提交实测",
                        {"复烘实测含水率": entry.get("复烘实测含水率"),
                         "复烘实测重量": entry.get("复烘实测重量")})
        return entry, (f"本盘实测已提交：含水率{entry.get('复烘实测含水率')}%，"
                       f"重量{entry.get('复烘实测重量', '未记')}")

    def _supplement_initial(self, entry: dict[str, Any], values: dict[str, Any]):
        status = entry.get("status")
        if status in ("已领取", "已终止"):
            return None, f"复烘处置条当前状态为「{status}」，不再接受初烘补报"
        corrected = store.parse_weight(values.get("质检补报含水率", "0"))
        if corrected <= 0:
            return None, "补报初烘须包含质检补报含水率"
        # 旧的初烘超标与复烘后实测各按该盘实际接受：补报另存，不改写初烘原始记录
        entry["质检补报含水率"] = str(corrected)
        note = values.get("补报说明")
        if note:
            entry["补报说明"] = str(note)
        store.log_event(self.MODULE, int(entry["id"]), "补报初烘",
                        {"质检补报含水率": entry["质检补报含水率"]})
        return entry, (f"质检补报含水率{corrected}%已登记，"
                       f"初烘原始记录{entry.get('初烘含水率')}%保持不变")

    def _confirm_handover(self, entry: dict[str, Any], values: dict[str, Any]):
        status = entry.get("status")
        if status == "可交接":
            return entry, f"{self.ENTITY}已处于「可交接」状态，无需重复操作"
        if status in ("已领取", "已终止"):
            return None, f"复烘处置条当前状态为「{status}」，不能确认交接"
        # 允许随动作一并提交当次实测
        if any(values.get(k) not in (None, "") for k in self.MEASURE_KEYS):
            err = self._record_measurement(entry, values)
            if err:
                return None, err
        moisture = store.parse_weight(entry.get("复烘实测含水率", "0"))
        if moisture <= 0:
            return None, "值班员尚未提交本盘实测含水率，不能确认交接"
        limit = store.parse_weight(entry.get("交接上限", "0"))
        if limit > 0 and moisture > limit:
            return None, f"本盘实测含水率{moisture}%高于该盘交接上限{limit}%，不能确认交接"
        entry["status"] = "可交接"
        entry["发料决定"] = "可发料"
        entry["交接确认人"] = entry.get("值班员", "")
        store.log_event(self.MODULE, int(entry["id"]), "确认交接",
                        {"from": status, "to": "可交接", "复烘实测含水率": moisture})
        return entry, f"值班员已按本盘实测{moisture}%确认可交接（交接上限{limit}%）"

    def _terminate(self, entry: dict[str, Any], values: dict[str, Any]):
        status = entry.get("status")
        if status == "已终止":
            return entry, f"{self.ENTITY}已处于「已终止」状态，无需重复操作"
        if status == "已领取":
            return None, "复烘产出已实际领取，不能终止复烘"
        # 允许随动作一并提交当前盘实测
        if any(values.get(k) not in (None, "") for k in self.MEASURE_KEYS):
            err = self._record_measurement(entry, values)
            if err:
                return None, err
        started = status in ("复烘中", "可交接")
        moisture = store.parse_weight(entry.get("复烘实测含水率", "0"))
        if started and moisture <= 0:
            return None, "复烘实物已影响原含水率，终止前须提交当前盘的实际测量"
        limit = store.parse_weight(entry.get("交接上限", "0"))
        if moisture > 0 and (limit <= 0 or moisture <= limit):
            entry["发料决定"] = "可发料"
            decision = f"按当前盘实测{moisture}%可发料"
        elif moisture > 0:
            entry["发料决定"] = "禁止发料"
            decision = f"当前盘实测{moisture}%高于交接上限{limit}%，禁止发料"
        else:
            entry["发料决定"] = "禁止发料"
            decision = "无本盘实测依据，禁止发料"
        entry["status"] = "已终止"
        entry["pending"] = False
        store.log_event(self.MODULE, int(entry["id"]), "终止复烘",
                        {"from": status, "to": "已终止", "发料决定": entry["发料决定"]})
        return entry, f"复烘已终止，{decision}"

    # ── 实测记录 ──

    def _record_measurement(self, entry: dict[str, Any], values: dict[str, Any]) -> str | None:
        """把值班员当次实测写到处置条上；返回错误信息或 None。

        复烘实测含水率未直接给出时，取中心/边缘实测的较高值（就高不就低）。
        """
        center = values.get("中心实测")
        edge = values.get("边缘实测")
        moisture_raw = values.get("复烘实测含水率")
        if moisture_raw in (None, "") and (center not in (None, "") or edge not in (None, "")):
            moisture = max(store.parse_weight(center), store.parse_weight(edge))
        else:
            moisture = store.parse_weight(moisture_raw)
        weight = values.get("复烘实测重量")
        if moisture <= 0 and weight in (None, ""):
            return "提交实测须包含本盘实测含水率（中心/边缘实测或复烘实测含水率）"
        if center not in (None, ""):
            entry["中心实测"] = str(center)
        if edge not in (None, ""):
            entry["边缘实测"] = str(edge)
        if moisture > 0:
            entry["复烘实测含水率"] = str(moisture)
        if weight not in (None, ""):
            entry["复烘实测重量"] = str(weight)
        return None

    # ── 分级领取 ──

    def collect(self, entry_id: int, grading_id: Any = None):
        """分级工位实际领取复烘产出；同一盘复烘完成最多实际领取一次。"""
        entry = store.find(self.MODULE, int(entry_id))
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        status = entry.get("status")
        if status == "已领取":
            if grading_id is not None and str(entry.get("领取分级记录ID")) == str(grading_id):
                return entry, "该盘复烘产出已由本分级记录实际领取，无需重复操作"
            return None, "同一盘复烘完成最多实际领取一次，该处置条产出已实际领取"
        if not self.issuable(entry):
            return None, f"复烘处置条当前状态为「{status}」，产出尚不能领取"
        collected = self.tray_collected(entry.get("干燥记录ID"))
        if collected is not None and int(collected["id"]) != int(entry["id"]):
            return None, "同一盘复烘完成最多实际领取一次，该盘复烘产出已实际领取"
        entry["status"] = "已领取"
        entry["pending"] = False
        if grading_id is not None:
            entry["领取分级记录ID"] = grading_id
        store.log_event(self.MODULE, int(entry["id"]), "领取产出",
                        {"grading_id": grading_id})
        return entry, "复烘产出已实际领取，分级记录保留当时含水率依据"

    def issuable(self, entry: dict[str, Any]) -> bool:
        """处置条当前是否允许发料：已确认可交接，或终止时凭实测判定可发料。"""
        if entry.get("status") == "可交接":
            return True
        return entry.get("status") == "已终止" and entry.get("发料决定") == "可发料"

    # ── 盘级查询 ──

    def find_for_tray(self, drying_id: Any) -> list[dict[str, Any]]:
        """获取同一盘（干燥记录）挂过的全部复烘处置条。"""
        return [r for r in store.rows(self.MODULE)
                if str(r.get("干燥记录ID", "")) == str(drying_id)]

    def active_for_tray(self, drying_id: Any) -> dict[str, Any] | None:
        """同一盘货当前挂着的有效处置条（草稿/复烘中/可交接）。"""
        for row in self.find_for_tray(drying_id):
            if row.get("status") in self.ACTIVE_STATUSES:
                return row
        return None

    def tray_collected(self, drying_id: Any) -> dict[str, Any] | None:
        """同一盘货已实际领取的处置条（如有）。"""
        for row in self.find_for_tray(drying_id):
            if row.get("status") == "已领取":
                return row
        return None

    def tray_review(self, drying_id: Any) -> dict[str, Any]:
        """班组回看：炉上处置条办理状态 + 分级是否实际收料。"""
        dispositions = self.find_for_tray(drying_id)
        gradings = [r for r in store.rows("grading")
                    if r.get("上游工序") == "drying"
                    and str(r.get("上游记录ID", "")) == str(drying_id)]
        collected = self.tray_collected(drying_id)
        active = self.active_for_tray(drying_id)
        return {
            "干燥记录ID": drying_id,
            "处置条": [dict(d) for d in dispositions],
            "有效处置条": dict(active) if active else None,
            "已实际领取": collected is not None,
            "领取处置条ID": collected.get("id") if collected else None,
            "分级实际收料": any(g.get("status") == "已确认" for g in gradings),
            "分级记录": [
                {"id": g.get("id"), "status": g.get("status"),
                 "含水率依据": g.get("含水率依据"), "上游重量": g.get("上游重量")}
                for g in gradings
            ],
        }
