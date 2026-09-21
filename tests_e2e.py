"""端到端冒烟测试：通过 HTTP API 覆盖主要业务路径。"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
PASS, FAIL = 0, 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ---------- 1. 示例①：氰化钾→盐酸→乙醇 ----------
print("【场景1】氰化物酸化（应第 2 步严重中止）")
s, r = call("POST", "/api/plans/1/simulate?persist=false")
check("HTTP 200", s == 200, s)
check("status=danger", r["status"] == "danger", r["status"])
check("halted=True", r["halted"] is True)
fd = r["first_danger"]
check("首次危险在第 1 步(0基)", fd["step"] == 1, fd)
check("冲突对象为第 0 步氰化钾", fd["conflicts_with_step"] == 0 and fd["conflicts_with_reagent"] == "氰化钾")
check("命中 R001", fd["rule_id"] == "R001")
check("含反应方程式", "HCN" in fd["reaction"])
check("含机理依据", len(fd["rationale"]) > 20)
check("含参考文献", len(fd["references"]) >= 1)
check("受影响步骤=[1,2]", r["affected_steps"] == [1, 2], r["affected_steps"])
check("阻断步骤=[2]", r["blocked_steps"] == [2], r["blocked_steps"])
states = [t["state"] for t in r["timeline"]]
check("时间轴状态 ok/danger/blocked", states == ["ok", "danger", "blocked"], states)
f0 = r["findings"][0]
check("finding 可追溯双向类别", f0["incoming_category"] == "strong_acid"
      and f0["existing_category"] == "cyanide", f0)

# ---------- 2. 示例②：铵盐→碱→次氯酸钠 ----------
print("【场景2】铵盐中和后加 84（先警示后严重）")
s, r = call("POST", "/api/plans/2/simulate?persist=false")
check("status=danger", r["status"] == "danger")
check("第 1 步 medium 警示后继续", r["timeline"][1]["state"] == "warning")
med = [f for f in r["findings"] if f["severity"] == "medium"]
check("R015 氨气警示命中", any(f["rule_id"] == "R015" for f in med), med)
check("首次危险在第 2 步 R016 氯胺", r["first_danger"]["step"] == 2
      and r["first_danger"]["rule_id"] == "R016", r["first_danger"])

# ---------- 3. 安全方案（无类别临时试剂 + 普通盐）----------
print("【场景3】安全配液（应 safe）")
s, reagents = call("GET", "/api/reagents")
nacl = next(x for x in reagents if x["name"] == "氯化钠")
plan = {
    "name": "测试-安全盐水",
    "steps": [
        {"order_index": 0, "reagent_id": nacl["id"], "time_offset_min": 0, "amount_ml": 100},
        {"order_index": 1, "reagent_name": "纯水", "categories": [], "time_offset_min": 5, "amount_ml": 50},
    ],
}
s, r = call("POST", "/api/plans/simulate-dry", plan)
check("dry-run 200", s == 200, s)
check("status=safe", r["status"] == "safe", r)
check("无 finding", r["summary"]["findings"] == 0)

# ---------- 4. 自定义临时类别 + 规则全链路 ----------
print("【场景4】自定义规则与临时试剂")
s, r = call("POST", "/api/rules", {
    "id": "C001", "source": "azide", "target": "strong_acid",
    "directional": False, "severity": "high",
    "title": "叠氮化物遇酸生成易爆叠氮酸",
    "reaction": "N3- + H+ -> HN3",
    "rationale": "叠氮酸剧毒且对震动敏感易爆炸。",
    "references": ["测试文献 1"],
})
check("创建自定义规则", s == 201, (s, r))
plan = {
    "name": "测试-叠氮",
    "steps": [
        {"order_index": 0, "reagent_name": "叠氮化钠", "categories": ["azide"]},
        {"order_index": 1, "reagent_name": "稀盐酸", "categories": ["strong_acid"]},
    ],
}
s, r = call("POST", "/api/plans/simulate-dry", plan)
check("自定义规则即时生效", r["status"] == "danger" and r["first_danger"]["rule_id"] == "C001")

# 有向规则：source 类别"被加入"已含 target 的体系时才触发（与加液顺序相关）
s, _ = call("POST", "/api/rules", {
    "id": "C002", "source": "cat_x", "target": "cat_y",
    "directional": True, "severity": "high", "title": "有向测试",
})
s, r = call("POST", "/api/plans/simulate-dry", {
    "name": "测试-有向反向",
    "steps": [
        {"order_index": 0, "reagent_name": "X", "categories": ["cat_x"]},
        {"order_index": 1, "reagent_name": "Y", "categories": ["cat_y"]},
    ],
})
check("有向规则：target 后加入不命中", r["status"] == "safe", r["status"])
s, r = call("POST", "/api/plans/simulate-dry", {
    "name": "测试-有向正向",
    "steps": [
        {"order_index": 0, "reagent_name": "Y", "categories": ["cat_y"]},
        {"order_index": 1, "reagent_name": "X", "categories": ["cat_x"]},
    ],
})
check("有向规则：source 后加入命中", r["status"] == "danger", r["status"])

# ---------- 5. 方案 CRUD + 持久化推演 + 导出 + 记录 ----------
print("【场景5】方案保存 / 推演落盘 / JSON 导出 / 记录")
s, saved = call("POST", "/api/plans", {
    "name": "测试-持久化",
    "description": "e2e",
    "steps": [
        {"order_index": 0, "reagent_name": "硫化钠临时", "categories": ["sulfide"], "time_offset_min": 0, "amount_ml": 10},
        {"order_index": 1, "reagent_name": "盐酸临时", "categories": ["strong_acid"], "time_offset_min": 2, "amount_ml": 5},
        {"order_index": 2, "reagent_name": "水", "categories": [], "time_offset_min": 9, "amount_ml": 100},
    ],
})
check("创建方案", s == 201 and saved["id"] > 0, (s, saved))
pid = saved["id"]
check("步骤类别快照回显", saved["steps"][0]["categories"] == ["sulfide"], saved["steps"][0])

s, r = call("POST", f"/api/plans/{pid}/simulate?persist=true")
check("推演成功", s == 200)
check("返回 run_id", r.get("run_id") is not None)
run_id = r["run_id"]
check("R002 H2S 命中", r["first_danger"]["rule_id"] == "R002")
check("第 3 步被阻断", r["blocked_steps"] == [2])

s, exported = call("GET", f"/api/plans/{pid}/export")
check("导出 JSON 含完整结构", s == 200
      and {"engine", "rule_revision", "status", "first_danger", "timeline", "findings"} <= set(exported),
      s)

s, runs = call("GET", "/api/runs")
check("推演记录列表", s == 200 and any(x["id"] == run_id for x in runs))
s, one = call("GET", f"/api/runs/{run_id}")
check("记录详情可取回", s == 200 and one["first_danger"]["rule_id"] == "R002")
s, one_exp = call("GET", f"/api/runs/{run_id}/export")
check("记录可导出", s == 200 and one_exp["run_id"] == run_id)

s, _ = call("PUT", f"/api/plans/{pid}", {"name": "测试-持久化-改名"})
check("方案更新", s == 200, s)
s, _ = call("DELETE", f"/api/plans/{pid}")
check("方案删除", s == 204, s)

# ---------- 6. 试剂 CRUD 与引用保护 ----------
print("【场景6】试剂档案与引用保护")
s, rg = call("POST", "/api/reagents", {
    "name": "测试试剂A", "cas": "1-2-3", "state": "liquid",
    "categories": ["oxidizer", "custom_cat"],
})
check("创建试剂", s == 201, (s, rg))
rid = rg["id"]
s, _ = call("POST", "/api/plans", {
    "name": "测试-占用试剂",
    "steps": [{"order_index": 0, "reagent_id": rid}],
})
pid2 = _["id"]
s, err = call("DELETE", f"/api/reagents/{rid}")
check("被引用试剂拒绝删除", s == 409, s)
call("DELETE", f"/api/plans/{pid2}")
s, _ = call("DELETE", f"/api/reagents/{rid}")
check("解除引用后可删除", s == 204, s)
s, err = call("POST", "/api/plans/simulate-dry", {"name": "x", "steps": []})
check("空步骤被拒绝", s == 422, s)

# ---------- 7. 规则停用/启用、内置保护 ----------
print("【场景7】规则维护")
s, _ = call("PATCH", "/api/rules/R001", {"active": False})
check("内置规则可停用", s == 200)
s, r = call("POST", "/api/plans/1/simulate?persist=false")
check("停用后不再命中 R001", not any(f["rule_id"] == "R001" for f in r["findings"]))
s, _ = call("PATCH", "/api/rules/R001", {"active": True})
check("重新启用", s == 200)
s, _ = call("DELETE", "/api/rules/R001")
check("内置规则不可删除", s == 409)
s, _ = call("DELETE", "/api/rules/C001")
s2, _ = call("DELETE", "/api/rules/C002")
check("自定义规则可删除", s == 204 and s2 == 204, (s, s2))

# ---------- 8. 版本差异（同一方案两次记录对齐比较） ----------
print("【场景8】推演记录版本差异")
s, p3 = call("POST", "/api/plans", {
    "name": "测试-版本差异方案",
    "steps": [
        {"order_index": 0, "reagent_name": "叠氮化钠X", "categories": ["azide"]},
        {"order_index": 1, "reagent_name": "稀盐酸X", "categories": ["strong_acid"]},
        {"order_index": 2, "reagent_name": "水", "categories": []},
    ],
})
pid3 = p3["id"]

s, _ = call("POST", "/api/rules", {
    "id": "C010", "source": "azide", "target": "strong_acid",
    "directional": False, "severity": "high",
    "title": "叠氮化物遇酸生成易爆叠氮酸",
    "reaction": "N3- + H+ -> HN3",
    "rationale": "叠氮酸剧毒且对震动敏感易爆炸。",
    "references": ["差异测试文献"],
})
check("创建测试规则 C010", s == 201, s)

s, ra = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_a = ra["run_id"]
check("第一次推演 danger", s == 200 and ra["status"] == "danger" and ra["blocked_steps"] == [2], (s, ra.get("status")))

call("PATCH", "/api/rules/C010", {"severity": "medium"})
s, rb = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_b = rb["run_id"]
check("降级后第二次推演 warning", s == 200 and rb["status"] == "warning" and rb["blocked_steps"] == [], (s, rb.get("status")))

s, d = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_b}")
check("差异接口 200", s == 200, s)
check("方案一致才返回", d.get("plan_id") == pid3, d.get("plan_id"))
check("两侧 run_id 正确", d["base"]["run_id"] == run_a and d["target"]["run_id"] == run_b)
check("状态 danger->warning", d["summary"]["status"] == {"base": "danger", "target": "warning", "changed": True})
check("首次危险消失", d["summary"]["first_danger"]["change"] == "removed")
check("阻断步骤 [2] 消失", d["summary"]["blocked_steps"]["removed"] == [2])
check("受影响步骤 [2] 消失", d["summary"]["affected_steps"]["removed"] == [2])
fc = d["summary"]["finding_changes"]
check("一条级别变化、无新增/消失", fc["severity"] == 1 and fc["added"] == 0 and fc["removed"] == 0, fc)
sev_entries = [f for f in d["findings"] if f["change"] == "severity"]
check("按步骤+规则对齐到 C010", len(sev_entries) == 1 and sev_entries[0]["rule_id"] == "C010"
      and sev_entries[0]["base_severity"] == "high" and sev_entries[0]["target_severity"] == "medium",
      sev_entries)
check("条目含两侧反应依据", sev_entries[0]["base"]["reaction"].startswith("N3-")
      and sev_entries[0]["target"]["rationale"].startswith("叠氮酸"), sev_entries)
check("命中计数 high 减少 1", any(x["key"] == "high" and x["delta"] == -1 for x in d["summary"]["counts"]),
      d["summary"]["counts"])

call("PATCH", "/api/rules/C010", {"active": False})
s, rc = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_c = rc["run_id"]
check("停用后第三次推演 safe", s == 200 and rc["status"] == "safe", (s, rc.get("status")))

s, d2 = call("GET", f"/api/runs/diff?base_id={run_b}&target_id={run_c}")
check("停用规则：命中消失", s == 200 and d2["summary"]["finding_changes"]["removed"] == 1
      and d2["summary"]["first_danger"]["change"] == "none"
      and d2["summary"]["status"]["changed"] and d2["summary"]["status"]["target"] == "safe",
      d2["summary"])
rm = [f for f in d2["findings"] if f["change"] == "removed"]
check("消失条目带规则 ID 与步骤", rm and rm[0]["rule_id"] == "C010" and rm[0]["incoming_step"] == 1, rm)

# 重新启用（恢复 high）后改机理文案 → 与 run_a 相比级别一致、依据变化
call("PATCH", "/api/rules/C010", {"active": True, "severity": "high"})
call("PATCH", "/api/rules/C010", {"rationale": "叠氮酸剧毒且对震动敏感易爆炸；2026 修订：常压下沸点仅 37℃。"})
s, rd = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_d = rd["run_id"]
s, d3 = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_d}")
basis = [f for f in d3["findings"] if f["change"] == "basis"]
check("级别不变机理变化 → basis", s == 200 and len(basis) == 1 and basis[0]["changed_fields"] == ["rationale"]
      and basis[0]["base_severity"] == "high" and basis[0]["target_severity"] == "high", basis)
check("两侧机理都可追溯", basis[0]["base"]["rationale"].startswith("叠氮酸剧毒")
      and "2026 修订" in basis[0]["target"]["rationale"], basis)

s, dexp = call("GET", f"/api/runs/diff/export?base_id={run_a}&target_id={run_b}")
check("差异 JSON 可导出", s == 200 and dexp.get("kind") == "labmix-run-diff"
      and dexp["base"]["run_id"] == run_a and dexp["target"]["run_id"] == run_b
      and "findings" in dexp and "summary" in dexp, s)

# 错误路径：同记录比较 / 记录缺失 / 跨方案
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_a}")
check("同一条记录 400", s == 400, s)
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id=999999")
check("对照记录缺失 404", s == 404, s)
s, e = call("GET", f"/api/runs/diff?base_id=999998&target_id={run_b}")
check("基准记录缺失 404", s == 404, s)
s, other = call("POST", "/api/plans/2/simulate?persist=true")
run_other = other["run_id"]
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_other}")
check("方案不一致 409", s == 409, s)
s, e = call("GET", f"/api/runs/diff/export?base_id={run_a}&target_id={run_other}")
check("导出接口同样拒绝跨方案 409", s == 409, s)
s, e = call("GET", "/api/runs/diff")
check("缺少参数 422", s == 422, s)

call("DELETE", f"/api/plans/{pid3}")
call("DELETE", f"/api/runs/C010")

# ---------- 9. 方案规则版本（草稿/发布/停用 + 判定口径 + 版本差异） ----------
print("【场景9】方案规则版本生命周期与判定口径")
s, pv = call("POST", "/api/plans", {
    "name": "测试-规则版本方案",
    "steps": [
        {"order_index": 0, "reagent_name": "氰化钾V", "categories": ["cyanide"]},
        {"order_index": 1, "reagent_name": "盐酸V", "categories": ["strong_acid"]},
        {"order_index": 2, "reagent_name": "水", "categories": []},
    ],
})
pvid = pv["id"]
check("创建方案", s == 201)
check("方案初始无生效版本", pv["effective_rule_version"] is None)

s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("无版本时按全局规则图判定", r["rule_scope"] == "global" and r["status"] == "danger", r.get("rule_scope"))

s, vl = call("GET", f"/api/plans/{pvid}/rule-versions")
check("版本列表初始为空", s == 200 and vl["versions"] == [] and vl["effective_version_id"] is None)

# 创建 v1（克隆全局启用规则，含严重级别依据）
s, v1 = call("POST", f"/api/plans/{pvid}/rule-versions", {"note": "首版"})
v1id = v1["id"]
check("创建草稿 v1", s == 201 and v1["version_no"] == 1 and v1["status"] == "draft")
r001 = next(x for x in v1["rules"] if x["id"] == "R001")
check("快照保留级别依据", r001["severity"] == "high" and "HCN" in r001["reaction"] and r001["references"])
check("草稿带创建/变更时间", v1["created_at"] and v1["updated_at"] and v1["published_at"] is None)

s, _ = call("POST", f"/api/plans/{pvid}/rule-versions", {})
check("同方案仅允许一个草稿", s == 409, s)

s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("草稿不参与判定（仍全局）", r["rule_scope"] == "global")

# 编辑草稿：R001 降为 medium
edited = v1["rules"]
for x in edited:
    if x["id"] == "R001":
        x["severity"] = "medium"
        x["rationale"] = x["rationale"] + "（本方案经工程防护降级为警示）"
s, v1u = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"rules": edited})
check("编辑草稿成功", s == 200
      and next(x for x in v1u["rules"] if x["id"] == "R001")["severity"] == "medium", s)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"rules": []})
check("空规则集草稿被拒", s == 409, s)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}",
            {"rules": [{**edited[0], "id": edited[0]["id"]}, {**edited[0], "id": edited[0]["id"]}]})
check("重复规则编号被拒", s == 409, s)

# 发布
s, v1p = call("POST", f"/api/plans/{pvid}/rule-versions/{v1id}/publish")
check("发布成功", s == 200 and v1p["status"] == "published" and v1p["published_at"] is not None)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"note": "x"})
check("已发布版本不可编辑", s == 409, s)

# 判定只读取生效版本
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=true")
v1_run = r["run_id"]
check("发布后判定读取版本快照", r["rule_scope"] == "plan_version"
      and r["rule_version_id"] == v1id and r["status"] == "warning", (r.get("rule_scope"), r.get("status")))
check("版本标识 plan{id}-rv1", r["rule_revision"] == f"plan{pvid}-rv1", r["rule_revision"])

# 全局规则停用不影响已发布快照
call("PATCH", "/api/rules/R001", {"active": False})
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("全局变更不影响版本快照", r["rule_scope"] == "plan_version" and r["status"] == "warning")
call("PATCH", "/api/rules/R001", {"active": True})

# 正式判定不允许指定版本；草稿预览允许
s, v2 = call("POST", f"/api/plans/{pvid}/rule-versions", {"note": "v2"})
v2id = v2["id"]
s, _ = call("POST", f"/api/plans/{pvid}/simulate?persist=true&rule_version_id={v2id}")
check("落盘判定不能指定版本", s == 422, s)
s, rp = call("POST", f"/api/plans/{pvid}/simulate?persist=false&rule_version_id={v2id}")
check("草稿可预览（不落盘）", s == 200 and rp["rule_version_id"] == v2id and rp.get("run_id") is None, s)

# 发布 v2 → v1 被替代
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/publish")
s, vl = call("GET", f"/api/plans/{pvid}/rule-versions")
check("新版本发布后旧版本自动替代", vl["effective_version_id"] == v2id
      and any(x["id"] == v1id and x["status"] == "superseded" for x in vl["versions"]))
s, pd = call("GET", f"/api/plans/{pvid}")
check("方案详情含当前生效状态", pd["effective_rule_version"]["id"] == v2id
      and pd["effective_rule_version"]["status_label"] == "生效中")

# 版本差异：与全局比，R001 high→medium
s, d = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base=global&target=" + str(v2id))
check("版本差异接口", s == 200 and d["kind"] == "labmix-rule-version-diff", s)
check("差异识别 R001 级别变化", d["changes"]["severity"] == 1
      and any(x["rule_id"] == "R001" and x["base_severity"] == "high"
              and x["target_severity"] == "medium" for x in d["rules"]), d["changes"])
rv1_entry = next(x for x in d["rules"] if x["rule_id"] == "R001")
check("差异条目保留两侧严重级别依据", "severity" in rv1_entry["changed_fields"]
      and "HCN" in rv1_entry["base"]["reaction"] and "降级为警示" in rv1_entry["target"]["rationale"])
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base={v1id}&target={v1id}")
check("相同版本比较 400", s == 400, s)
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base=999999&target=global")
check("版本不存在 404", s == 404, s)

# 停用：不得继续参与判定
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/deactivate")
check("停用成功", s == 200)
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("停用后回落全局规则图", r["rule_scope"] == "global" and r["status"] == "danger",
      (r.get("rule_scope"), r.get("status")))
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/deactivate")
check("非生效版本不可停用", s == 409, s)
s, _ = call("DELETE", f"/api/plans/{pvid}/rule-versions/{v2id}")
check("已发布版本不可删除", s == 409, s)

# 历史记录保留版本关联
s, runs = call("GET", "/api/runs")
rec = next(x for x in runs if x["id"] == v1_run)
check("推演记录关联生效版本", rec["rule_version_id"] == v1id, rec)

# 跨方案隔离 & 级联删除
s, otherp = call("POST", "/api/plans", {"name": "测试-版本隔离方案",
    "steps": [{"order_index": 0, "reagent_name": "水", "categories": []}]})
s, _ = call("GET", f"/api/plans/{otherp['id']}/rule-versions/{v1id}")
check("跨方案不可访问版本", s == 404, s)
s, _ = call("DELETE", f"/api/plans/{pvid}")
check("删除方案", s == 204, s)
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions")
check("方案删除后版本级联清除", s == 404, s)
call("DELETE", f"/api/plans/{otherp['id']}")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
