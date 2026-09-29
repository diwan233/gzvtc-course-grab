#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 diwan233
# SPDX-License-Identifier: GPL-3.0-only
#
# 本程序是自由软件: 可自由使用、修改、分发, 但衍生作品必须同样以 GPL-3.0
# 开源并保留本声明, 不得附加额外限制。本程序不含任何担保, 完整条款见 LICENSE。
"""离线自检: 验证"响应码 -> 处置"映射与选课池逻辑, 不联网。

运行方式: python selfTest.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("main", os.path.join(HERE, "main.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

ok = True


def check(cond, msg):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        ok = False


print("=" * 72)
print("1) 选课响应码 -> 处置映射 (VERDICT 覆盖度)")
print("=" * 72)
expect = {
    "ok": "success",
    "full": "retry",            # 满员 -> 保留在池中继续重试
    "fail": "retry",            # 满员或已选过 -> 保留在池中继续重试
    "selected": "selected",     # 已选过 -> 单独判定
    "busy": "drop",             # 时间冲突 -> 剔除出池
    "stop": "drop",             # 已停开 -> 剔除出池
    "start": "drop",            # 已开课 -> 剔除出池
    "morescore": "fatal",       # 本学期学分上限 -> 整体停止
    "moreallscore": "fatal",    # 三年学分上限 -> 整体停止
    "timeout": "retry",         # 本轮无响应 -> 保留在池中继续重试
}
for code, want in expect.items():
    got, desc = g.VERDICT.get(code, ("retry", f"未知响应 {code!r}"))
    check(got == want, f"{code:12s} -> {got:8s} (期望 {want:8s}) | {desc}")

print("\n  未知响应码(如服务端新增码) 兜底:")
for weird in ["6666", "whatever", "错误"]:
    got, desc = g.VERDICT.get(weird, ("retry", f"未知响应 {weird!r}"))
    check(got == "retry", f"{weird:12s} -> {got} | {desc}  (必须保留在池中重试, 不可丢弃)")

print("\n" + "=" * 72)
print("2) 降级空壳识别 (list_ready)")
print("=" * 72)
shell = '{"pageNum":1,"pageSize":0,"size":0,"total":0,"list":[]}'
stub = ('{"pageNum":1,"pageSize":1,"total":1,"list":[{"courseName":null,"studentCount":"10/15",'
        '"currentSemester":"2020-2021学年第一学期","teachingClassName":"选修课-中西面点工艺学1班"}]}')
real = '{"pageNum":1,"pageSize":10,"total":3,"list":[{"id":1,"courseName":"羽毛球（一 ）"}]}'
check(g.McrpClient.list_ready(real) is True, "有真课程的响应 -> 视为就绪")
check(g.McrpClient.list_ready(shell) is False, "空壳(pageSize=0/total=0) -> 视为未就绪(继续重试)")
check(g.McrpClient.list_ready(stub) is False, "占位行(courseName=null 的旧数据) -> 视为未就绪(继续重试)")
check(g.McrpClient.list_ready('{"code":"6666","msg":"访问高峰期"}') is False, "6666 -> 未就绪")

print("\n" + "=" * 72)
print("2b) /go 闸门欢迎页识别 (_is_gate_page)")
print("=" * 72)
# 实测样本(全新登录但没过 /go 时, list 接口返回的就是这个欢迎页)
gate_html = ('<!DOCTYPE html>\r\n\r\n\r\n \r\n \r\n \r\n<html>\r\n\r\n\t<head>\r\n'
             '\t\t<meta charset="utf-8" />\r\n\t</head>\r\n\t<body>\r\n'
             '\t\t<p>已注册</p>\r\n'
             '\t\t<a href="go" id="login">阅读清楚，并进入学生门户</a>\r\n'
             '\t</body>\r\n</html>')
other_html = '<!DOCTYPE html>\n<html><body><h1>404 Not Found</h1></body></html>'
check(g.McrpClient._is_gate_page(gate_html) is True,
      "欢迎页 HTML -> 判定为『被闸门挡住』(需放行 /go)")
check(g.McrpClient._is_gate_page(real) is False, "正常 JSON 列表 -> 不是闸门页")
check(g.McrpClient._is_gate_page('{"code":"6666","msg":"访问高峰期"}') is False, "6666 -> 不是闸门页")
check(g.McrpClient._is_gate_page("") is False, "空响应 -> 不是闸门页")
check(g.McrpClient._is_gate_page("__ERR__ TimeoutError") is False, "网络错误 -> 不是闸门页")
check(g.McrpClient._is_gate_page(other_html) is False,
      "其他 HTML(如 404) -> 不误判为闸门页, 避免多余的 /go 请求")
check(g.McrpClient.list_ready("<html>欢迎页</html>") is False, "HTML -> 未就绪")
check(g.McrpClient.list_ready("") is False, "空响应 -> 未就绪")

print("\n" + "=" * 72)
print("3) 人数解析与选课池构建")
print("=" * 72)
check(g.vacancy("16/17") == (16, 17), "vacancy('16/17') = (16, 17)")
check(g.vacancy("17/17") == (17, 17), "vacancy('17/17') = (17, 17)")
check(g.vacancy("") is None, "vacancy('') = None")
check(g.vacancy(None) is None, "vacancy(None) = None")

courses = [
    {"id": 1, "courseName": "羽毛球（一 ）", "studentCount": "17/17"},   # 满
    {"id": 2, "courseName": "羽毛球（一 ）", "studentCount": "16/17"},   # 1 空位
    {"id": 3, "courseName": "网球（一 ）", "studentCount": "3/17"},      # 不匹配关键字
    {"id": 4, "courseName": "羽毛球（一 ）", "studentCount": "0/17"},    # 17 空位
]
pool = g.build_pool(courses, ["羽毛球"], False)
ids = [c["id"] for _, c in pool]
check(1 in ids and 2 in ids and 4 in ids, "默认(整池): 满员的班也进池")
check(3 not in ids, "默认(整池): 不匹配关键字的班不进池")
check(ids[0] == 4 and ids[-1] == 1, f"排序: 空位多的排前, 满员排最后 -> {ids}")
check(all(k == "羽毛球" for k, _ in pool), "池内每项都带上了命中的关键字")

pool2 = g.build_pool(courses, ["羽毛球"], True)
check([c["id"] for _, c in pool2] == [4, 2],
      f"onlyWithVacancy=True: 只留有空位的 -> {[c['id'] for _, c in pool2]}")

print("\n" + "=" * 72)
print("4) 多关键字匹配与轮转交错（尤克里里 / 瑜伽）")
print("=" * 72)
multi = [
    {"id": 11, "courseName": "尤克里里（一 ）", "studentCount": "10/15"},
    {"id": 12, "courseName": "尤克里里（一 ）", "studentCount": "5/15"},
    {"id": 13, "courseName": "瑜伽（一 ）", "studentCount": "8/20"},
    {"id": 14, "courseName": "瑜伽（一 ）", "studentCount": "2/20"},
    {"id": 15, "courseName": "羽毛球（一 ）", "studentCount": "1/17"},   # 不在关键字里
]
KW = ["尤克里里", "瑜伽"]
pool3 = g.build_pool(multi, KW, False)
seq = [(k, c["id"]) for k, c in pool3]
check(15 not in [c["id"] for _, c in pool3], "两个关键字之外的课不进池")
check(len(pool3) == 4, f"两个关键字各 2 个班都进池 -> {len(pool3)} 门")
check([k for k, _ in seq] == ["尤克里里", "瑜伽", "尤克里里", "瑜伽"],
      f"按关键字轮转交错(不是把某一类排前面) -> {seq}")
check(g.match_keyword({"courseName": "瑜伽（一 ）"}, KW) == "瑜伽", "match_keyword 命中 瑜伽")
check(g.match_keyword({"courseName": "尤克里里（一 ）"}, KW) == "尤克里里", "match_keyword 命中 尤克里里")
check(g.match_keyword({"courseName": "网球（一 ）"}, KW) is None, "match_keyword 未命中返回 None")
pool4 = g.build_pool(multi, KW, True)
check(len(pool4) == 4, f"onlyWithVacancy=True 时这 4 个班都有空位 -> {len(pool4)} 门")

print("\n" + "=" * 72)
print("5) 每个关键字匹配到的课全部入池（尤克里里 / 瑜伽 / 微生物）")
print("=" * 72)
KW3 = ["尤克里里", "瑜伽", "微生物"]
multi2 = multi + [
    {"id": 16, "courseName": "微生物学基础", "studentCount": "30/30"},   # 满员
    {"id": 17, "courseName": "微生物与人类", "studentCount": "12/40"},   # 有空位
]
pool5 = g.build_pool(multi2, KW3, False)
ids5 = [c["id"] for _, c in pool5]
check(len(pool5) == 6, f"三个关键字匹配到的 6 门课全部入池 -> {len(pool5)} 门")
check(16 in ids5, "满员的微生物班也在池里（只排最后，不剔除）")
check(15 not in ids5, "第三个关键字之外的羽毛球课不进池")
st = g.keyword_stats(multi2, KW3, False)
check(st.get("微生物") == (2, 2), f"微生物: 匹配 2 门 / 入池 2 门 -> {st.get('微生物')}")
check(st.get("尤克里里") == (2, 2) and st.get("瑜伽") == (2, 2),
      f"尤克里里/瑜伽 也都是全量入池 -> {st.get('尤克里里')}, {st.get('瑜伽')}")
st2 = g.keyword_stats(multi2, KW3, True)
check(st2.get("微生物") == (2, 1),
      f"onlyWithVacancy=True 时满员的微生物班被排除 -> {st2.get('微生物')}")
seq3 = [k for k, _ in pool5]
check(seq3[:3] == KW3, f"三个关键字仍按轮转交错 -> 前三项 {seq3[:3]}")
check(seq3 == ["尤克里里", "瑜伽", "微生物", "尤克里里", "瑜伽", "微生物"],
      f"完整交错顺序 -> {seq3}")

print("\n" + "=" * 72)
print("6) 退出条件: 每个类型都抢到才算完成")
print("=" * 72)
TG = ["尤克里里", "瑜伽", "微生物"]
check(g.all_achieved(TG, {}) is False, "什么都没抢到 -> 未完成")
check(g.all_achieved(TG, {"尤克里里": {}}) is False, "只抢到 1/3 -> 未完成, 继续抢")
check(g.all_achieved(TG, {"尤克里里": {}, "瑜伽": {}}) is False, "抢到 2/3 -> 未完成, 继续抢")
check(g.all_achieved(TG, {"尤克里里": {}, "瑜伽": {}, "微生物": {}}) is True,
      "3/3 全抢到 -> 完成, 可以退出")
check(g.all_achieved([""], {"": {}}) is True, "未配置关键字(单一类型)时, 抢到一门即完成")

print("\n" + "=" * 72)
print("7) 配置加载 load_config (只支持 TOML)")
print("=" * 72)
_tmp = tempfile.mkdtemp(prefix="gzvtc-coursegrab-cfg-")
_toml = os.path.join(_tmp, "c.toml")
with open(_toml, "w", encoding="utf-8") as f:
    f.write("""# 中文注释
[account]
studyNumber = "S"   # 行尾注释

[target]
keywords = ["A", "B"]
semester = "2026"
""")

ct = g.load_config(_toml)
check(ct["account"]["studyNumber"] == "S", "TOML: 值读取正确")
check('# 中文注释' not in str(ct), "TOML: # 注释不会被当成配置键")
check(ct["target"]["keywords"] == ["A", "B"], "TOML: 行尾注释不影响解析")
check(ct["concurrency"]["threads"] == 8, "缺省值补齐: concurrency.threads -> 8")
check(ct["concurrency"]["maxTotalAttempts"] == 2000, "缺省值补齐: maxTotalAttempts")
check(ct["session"]["cookieFile"] == "cookies.json", "缺省值补齐: session.cookieFile")
check(ct["session"]["reuseCookies"] is True, "缺省值补齐: reuseCookies=true")
check(ct["retry"]["timeoutSeconds"] == 6, "缺省值补齐: retry.timeoutSeconds")
check(ct["behavior"]["requireAllKeywords"] is True, "缺省值补齐: requireAllKeywords=true")
check(ct["behavior"]["forceRelogin"] is False, "缺省值补齐: forceRelogin=false")
check(ct["behavior"]["waitWhenClosed"] is True, "缺省值补齐: waitWhenClosed=true")
check(ct["retry"]["closedWaitSeconds"] == 30, "缺省值补齐: retry.closedWaitSeconds")
check(ct["retry"]["refreshList"] is True, "缺省值补齐: retry.refreshList=true")
check(ct["retry"]["refreshListEveryCycles"] == 1, "缺省值补齐: refreshListEveryCycles=1")
check(ct["target"]["onlyWithVacancy"] is False, "缺省值补齐: onlyWithVacancy=false")
check(ct["account"].get("password", "") == "", "缺省值补齐: account 段整体缺失时也不报错")

# 配置内容非法时必须明确报错, 不可静默当作空配置继续运行
_bad = os.path.join(_tmp, "bad.toml")
with open(_bad, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法 TOML }")
try:
    g.load_config(_bad)
    check(False, "非法配置应报错")
except Exception:
    check(True, "非法配置 -> 解析报错(main() 会提示「配置文件解析失败」)")

check(g.here_path("x.toml") == os.path.join(HERE, "x.toml"),
      "相对路径 -> 按脚本所在目录解析(与 cwd 无关)")
check(g.here_path("C:/a/b.toml").replace("\\", "/") == "C:/a/b.toml", "绝对路径 -> 原样返回")

# dry-run 存的课程列表要能原样读回
_dump = os.path.join(_tmp, "debug", "list.json")
g.save_list([{"id": 1, "courseName": "瑜伽"}], "2026-2027学年第一学期", name=_dump)
with open(_dump, encoding="utf-8") as f:
    _back = json.load(f)
check(_back["count"] == 1, "列表落盘: count 记录条数")
check(_back["semester"] == "2026-2027学年第一学期", "列表落盘: semester 一并写入")
check(_back["list"][0]["courseName"] == "\u745c\u4f3d", "列表落盘: 中文原样保留(ensure_ascii=False)")
check(os.path.isfile(_dump), "列表落盘: 目录不存在时自动建(debug/)")
try:
    g.load_config(os.path.join(_tmp, "nope.toml"))
    check(False, "读不存在的配置应报错")
except FileNotFoundError:
    check(True, "读不存在的配置 -> FileNotFoundError (main() 会给出友好提示)")

# 仓库里必须有配置模板 —— 它是本地 config.toml 的来源
if not os.path.isfile(os.path.join(HERE, "config.example.toml")):
    print("  ❌ 缺配置文件模板 config.example.toml (应随仓库一起存在)")
    sys.exit(1)

import shutil
shutil.rmtree(_tmp, ignore_errors=True)

print("\n" + "=" * 72)
print("8) 选课时段探测 (page_state: 现在能不能选课)")
print("=" * 72)
# 实测样本: 非选课时段服务端渲染的极简页(整页约 1.2KB, 无 Vue / 无列表脚本)
closed_page = ('<!DOCTYPE html>\n<html>\n\t<head>\n\t\t<meta charset="utf-8" />\n'
               '\t\t<title>网上选课</title>\n\t</head>\n\t<body>\n'
               '\t<header class="mui-bar mui-bar-nav">\n'
               '\t\t<p class="text-align-center font-size-mid">网上选课</p>\n'
               '\t</header>\n'
               '\t<div class="mui-content">\n\t\t<div class="msg">\n'
               '\t\t\t<p class="mcrp-font-memo-color">'
               '<i class="iconfont icon-info-circle"></i></p>\n'
               '\t\t\t<p class="mcrp-font-title-color">当前时间不可选课</p>\n'
               '\t\t</div>\n\t</div>\n'
               '\t<script src="/student/static/mui/js/mui.js"></script>\n'
               '\t</body>\n</html>')
# 可选课时返回的完整选课页(节选)
open_page = ('<title>网上选课</title><ul id="courseList" class="mui-table-view">'
             '</ul><script>function pulldownRefresh(){}</script>')
check(g.McrpClient.page_state(closed_page) == "closed",
      "非选课时段的服务端极简页 -> closed")
check(g.McrpClient.page_state(open_page) == "open",
      "可选课时的完整选课页 -> open")
check("pulldownRefresh" not in closed_page,
      "非选课时段页面里没列表脚本(所以只能靠「不可选课」这句话判)")
check(g.McrpClient.CLOSED_MARK not in open_page, "两张页面靠标记区分, 不会互相误判")
check(g.McrpClient.page_state("") is None, "空响应 -> None(判断不了, 稍后重试)")
check(g.McrpClient.page_state("__ERR__ TimeoutError") is None, "网络错误 -> None")
check(g.McrpClient.page_state('{"code":"6666","msg":"访问高峰期"}') is None,
      "6666 限流 -> None(不能据此判为已关闭)")
check(g.McrpClient.page_state('<form id="login-form">请输入密码</form>') == "login",
      "登录页 -> login(会话过期, 等待期间要补登录)")

print("\n" + "=" * 72)
print("9) 列表刷新节奏 (should_refresh / next_refresh_cycle)")
print("=" * 72)
sr = g.should_refresh
check(sr(1, 1, True) is False, "第 1 轮不刷(刚启动拉过)")
check(sr(1, 5, True) is False, "第 1 轮不刷(无论 every 是多少)")
check(sr(2, 1, True) and sr(3, 1, True) and sr(9, 1, True),
      "every=1 -> 第 2 轮起每轮都刷(默认)")
check(sr(2, 2, True) is True and sr(3, 2, True) is False
      and sr(4, 2, True) is True and sr(6, 2, True) is True,
      "every=2 -> 第 2/4/6… 轮刷(轮次号被 every 整除)")
check(sr(3, 3, True) is True and sr(4, 3, True) is False
      and sr(5, 3, True) is False and sr(6, 3, True) is True,
      "every=3 -> 第 3/6/9… 轮刷(并非第 2 轮)")
check(sr(5, 5, True) is True and sr(2, 5, True) is False and sr(4, 5, True) is False,
      "every=5 -> 第 5/10/15… 轮刷(第 2 轮不刷)")
check(sr(2, 1, False) is False and sr(99, 1, False) is False,
      "refreshList=false -> 永不刷新(只用启动时拉取的那次)")
check(sr(1, 1, False) is False, "关掉开关时第 1 轮也不刷(启动时已拉取)")
check(sr(2, 0, True) is True, "every 填 0 也能健壮处理(当作 1, 不会除零/卡死)")
check(sr(2, -5, True) is True, "every 填负数也健壮处理(按 1 处理)")

nx = g.next_refresh_cycle
check(nx(1, 5, True) == 5, "第 1 轮后 -> 下次刷新在第 5 轮")
check(nx(2, 5, True) == 5, "第 2 轮后 -> 仍是第 5 轮")
check(nx(5, 5, True) == 10, "第 5 轮后 -> 第 10 轮(不会预告成第 5 轮)")
check(nx(1, 1, True) == 2, "every=1 -> 下次就是下一轮")
check(nx(9, 1, False) is None, "不刷新列表 -> 返回 None(日志显示\"不刷新列表\")")

print("\n" + "=" * 72)
print("全部通过 ✅" if ok else "存在失败 ❌")
print("=" * 72)
sys.exit(0 if ok else 1)
