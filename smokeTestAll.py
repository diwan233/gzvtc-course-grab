#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 diwan233
# SPDX-License-Identifier: GPL-3.0-only
#
# 本程序是自由软件: 可自由使用、修改、分发, 但衍生作品必须同样以 GPL-3.0
# 开源并保留本声明, 不得附加额外限制。本程序不含任何担保, 完整条款见 LICENSE。
"""离线冒烟测试: 用假客户端完整执行一遍 main.run() 的循环, 验证退出条件。

不联网、不选课。运行方式: python smokeTestAll.py
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("main", os.path.join(HERE, "main.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

LOGS = []
g.log = lambda m: (LOGS.append(str(m)))

GATE_CALLS = []          # 记录 run() 是否调用了 pass_gate (懒加载闸门后应为空)
LIST_CALLS = []          # 记录每次拉取课程列表 (验证刷新节奏开关)


class FakeClient:
    """假客户端: 前 fail_times 次对某班回 'full', 之后回 'ok'; never_ok 里的永远回 'full'。

    busy_once 里的班: 第一次回 'busy'(时间冲突 -> 被剔除出池), 之后照常。
    """

    def __init__(self, cfg, courses, fail_times=1, never_ok=(), state="open",
                 busy_once=(), list_fail_at=()):
        self.courses = courses
        self.fail_times = fail_times
        self.never_ok = set(never_ok)
        self.busy_once = set(busy_once)
        self.list_fail_at = set(list_fail_at)   # 第 N 次拉列表返回 None(模拟限流/降级)
        self.state = state
        self.tries = {}
        self.list_calls = 0
        self.verbose = bool((cfg.get("behavior") or {}).get("verbose"))
        self.n_req = self.n_busy = self.n_err = self.n_stale = 0

    # --- 主流程用到的接口 ---
    def ensure_session(self):
        g.log("[OK] 复用缓存会话(假)")
        return True

    def pass_gate(self):
        GATE_CALLS.append(1)
        g.log("[OK] 闸门已放行(假)")
        return True

    def load_courses(self, semester, belonging="", loud=True):
        LIST_CALLS.append(1)
        self.list_calls += 1
        if self.list_calls in self.list_fail_at:
            return None
        return self.courses

    def selection_state(self, rounds=3):
        """假客户端: 默认认为选课已开放。"""
        return self.state

    def select(self, course_id):
        self.tries[course_id] = self.tries.get(course_id, 0) + 1
        n = self.tries[course_id]
        if course_id in self.busy_once and n == 1:
            return "busy", n
        if course_id in self.never_ok:
            return "full", n
        if n > self.fail_times:
            return "ok", n
        return "full", n

    def confirm_selected(self, course, semester, belonging=""):
        g.log(f"[OK] 复核(假): {course.get('courseName')} 人数 {course.get('studentCount')} -> 满")


def course(cid, name, count="0/20"):
    return {"id": cid, "courseName": name, "teachingClassName": f"选修课-{name}1班",
            "studentCount": count, "teacherNames": "老师(1)", "courseMemo": "周一 1-2节"}


def make_cfg(keywords, require_all=True, max_run_minutes=0, refresh_list=True,
             refresh_every=1):
    return {
        "account": {"studyNumber": "x", "password": "y"},
        "target": {"keywords": keywords, "belonging": "", "semester": "S",
                   "onlyWithVacancy": False},
        "session": {"cookieFile": "cookies.json", "reuseCookies": True,
                    "cookieMaxAgeMinutes": 0},
        "retry": {"busyGapMs": 1, "listIntervalMs": 1, "timeoutSeconds": 1,
                  "maxRunMinutes": max_run_minutes, "closedWaitSeconds": 1,
                  "refreshList": refresh_list,
                  "refreshListEveryCycles": refresh_every},
        "concurrency": {"threads": 4, "maxTotalAttempts": 8},
        "behavior": {"dryRun": False, "verbose": False, "forceRelogin": False,
                     "requireAllKeywords": require_all,
                     "waitWhenClosed": True,
                     "treatSelectedAsSuccess": True},
    }


def run_case(title, keywords, courses, require_all, max_run_minutes=0, never_ok=(),
             state="open", refresh_list=True, refresh_every=1, busy_once=(),
             list_fail_at=()):
    LOGS.clear()
    GATE_CALLS.clear()
    LIST_CALLS.clear()
    cfg = make_cfg(keywords, require_all, max_run_minutes, refresh_list, refresh_every)
    real = g.McrpClient
    g.McrpClient = lambda c: FakeClient(c, courses, fail_times=1, never_ok=never_ok,
                                        state=state, busy_once=busy_once,
                                        list_fail_at=list_fail_at)
    try:
        code = g.run(cfg, types.SimpleNamespace(keyword=None, dry_run=False))
    finally:
        g.McrpClient = real
    print(f"\n### {title}  -> 退出码 {code}")
    for line in LOGS:
        print("   " + line)
    return code, "\n".join(LOGS)


ok = True


def check(cond, msg):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        ok = False


ALL = [course(101, "尤克里里（一 ）"), course(102, "瑜伽（一 ）"), course(103, "微生物学基础")]

print("=" * 78)
print("用例 1: 三个类型都能抢到 -> 必须全部抢到才退出 (requireAllKeywords=true)")
print("=" * 78)
code, log = run_case("三类型逐个抢到", ["尤克里里", "瑜伽", "微生物"], ALL, True)
check(code == 0, "退出码 0")
check("3/3 个类型都已抢到" in log, "日志出现『3/3 个类型都已抢到』")
check(log.count("🎉 选课成功!") == 3, f"共成功 3 次(每个类型各一次) -> {log.count('🎉 选课成功!')}")
check(log.index("尤克里里") < log.index("微生物"), "按池顺序依次达成")
check("(未抢到)" not in log, "没有未抢到的类型")
# 懒加载闸门: 缓存会话能直接拿到列表时, run() 不应再去走 /go
check(not GATE_CALLS, "run() 未调用 pass_gate (懒加载闸门生效, 省掉 /go 开销)")
check("[OK] 闸门已放行(假)" not in log, "日志里没有『闸门已放行』这一步")
# 默认 refreshListEveryCycles=1 -> 启动 1 次 + 第 2 轮起每轮 1 次
check(len(LIST_CALLS) >= 2, f"默认每轮刷列表 -> 共拉了 {len(LIST_CALLS)} 次")
check("刷新课程列表" in log, "日志里能看到『刷新课程列表』触发")
check("列表已刷新: 3 门" in log, "刷新成功 -> 日志给出刷新后的条数")

print("\n" + "=" * 78)
print("用例 2: 只有一个类型能抢到 -> 不能提前退出(应持续运行, 到时限才结束)")
print("=" * 78)
code, log = run_case("只抢得到尤克里里", ["尤克里里", "瑜伽", "微生物"], ALL, True,
                     max_run_minutes=0.03, never_ok=[102, 103])
check("🎉 选课成功!" in log, "尤克里里 抢课成功")
check("未能全部抢到, 已结束" in log, "因为未全部达成, 走的是『未能全部抢到』收尾分支")
check("[❌] 瑜伽" in log and "[❌] 微生物" in log, "收尾报告里瑜伽/微生物标记为未抢到")
check("🎉 任务完成" not in log, "没有误报『任务完成』")

print("\n" + "=" * 78)
print("用例 3: requireAllKeywords=false -> 抢到一门就退出(旧行为)")
print("=" * 78)
code, log = run_case("抢到一门即停", ["尤克里里", "瑜伽", "微生物"], ALL, False)
check(code == 0, "退出码 0")
check(log.count("🎉 选课成功!") == 1, f"只成功 1 次就退出 -> {log.count('🎉 选课成功!')}")

print("\n" + "=" * 78)
print("用例 4: 当前时间不可选课 -> 轮询等待, 到时限才结束")
print("=" * 78)
code, log = run_case("非选课时段", ["尤克里里"], ALL, True,
                     max_run_minutes=0.03, state="closed")
check(code == 1, f"退出码 1 -> {code}")
check("当前时间不可选课" in log, "日志明确提示『当前时间不可选课』")
check("秒后再探测" in log, "日志提示会隔一段时间重试")
check("等到时限仍未开放, 结束" in log, "到时限后才放弃")
check(log.count("当前时间不可选课") >= 2, "确实重试了多次, 不是一次就放弃")
check("🎉 选课成功!" not in log, "不可选课时没有做任何选课尝试")
check("[OK] 列表直接可用" not in log, "不可选课时完全没有请求课程列表")

print("\n" + "=" * 78)
print("用例 5: refreshList=false -> 只启动时拉一次列表, 之后不再刷新")
print("=" * 78)
code, log = run_case("关掉列表刷新", ["尤克里里", "瑜伽", "微生物"], ALL, True,
                     refresh_list=False)
check(code == 0, "退出码 0(关掉刷新不影响抢课)")
check(len(LIST_CALLS) == 1, f"只拉了 1 次列表(启动那次) -> {len(LIST_CALLS)}")
check("刷新课程列表" not in log, "日志里没有『刷新课程列表』")

print("\n" + "=" * 78)
print("用例 6: 首轮被判时间冲突剔除出池 -> 重拉列表后废弃记录作废, 该班重新入池")
print("=" * 78)
code, log = run_case("废弃记录在刷新后作废", ["尤克里里"], ALL, True,
                     refresh_every=1, busy_once=[101])
check(code == 0, f"退出码 0 -> {code}")
check("剔除出池" in log, "第 1 轮把该班剔除出池(时间冲突)")
check("清空废弃记录" in log, "重拉列表时清空了废弃记录")
check("🎉 选课成功!" in log, "被剔除的班下一轮重新入池并抢课成功")
check(log.index("剔除出池") < log.index("清空废弃记录"), "顺序正确: 先剔除, 后清空")

print("\n" + "=" * 78)
print("用例 7: 重拉列表遇到限流 -> 必须明确提示, 并沿用上一份列表继续运行")
print("=" * 78)
code, log = run_case("刷新失败不静默", ["尤克里里", "瑜伽", "微生物"], ALL, True,
                     refresh_every=1, list_fail_at=[2])
check(code == 0, f"退出码 0(刷新失败不该影响抢课) -> {code}")
check("刷新失败" in log, "日志明确提示『刷新失败』")
check("沿用上一份" in log, "日志说明沿用了上一份列表")
check("🎉 选课成功!" in log, "沿用旧列表仍能抢课成功")

print("\n" + "=" * 78)
print("全部通过 ✅" if ok else "存在失败 ❌")
print("=" * 78)
sys.exit(0 if ok else 1)