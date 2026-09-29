#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 diwan233
# SPDX-License-Identifier: GPL-3.0-only
#
# 本程序是自由软件: 可自由使用、修改、分发, 但衍生作品必须同样以 GPL-3.0
# 开源并保留本声明, 不得附加额外限制。本程序不含任何担保, 完整条款见 LICENSE。
"""
广州工程技术职业学院学生门户 - 网上选课抢课脚本 (纯标准库, 零依赖)

策略:
  1. 会话: 优先复用 cookies.json 里缓存的 JSESSIONID, 文件为空/过期/失效才登录
  2. 登录 = 直接 POST 表单
  3. 直接请求课程列表接口 -> 关键字匹配 -> 所有匹配的课程全部进**选课池**(有空位的排前面)
     (被欢迎页闸门挡住时才按需放行 /go, 见设计要点 4)
  4. 以整个池为单位循环: 池内逐个尝试, 失败立即跳转下一个; 一轮结束后刷新列表继续
  5. 每个关键字(类型)都抢到一门才退出(behavior.requireAllKeywords, 默认 true)

并发说明(解决"每次重试等待过久"的问题):
  所有请求都是**多线程并发**发出的 —— 一轮同时打 threads 路(见 config 的 concurrency.threads),
  一轮内收齐全部响应再决策; 整轮均被 6666 拦截则立即开始下一轮, 不串行等待往返。
  实测串行时单次失败的往返可能阻塞 6s, 并发后吞吐提升约 threads 倍。
  选课时若同一轮内任一路取得 'ok', 一律判定为 'ok'。

设计要点(均为实测结论, 改动前请先读):
  1. 服务端限流返回 {"code":"6666","msg":"访问高峰期..."}, 属**随机性拦截**,
     实测连发 8 次仅 1 次通过。必须重试, 不可当作"选课失败"而放弃。
  2. 会话 Cookie 是 HttpOnly 的 JSESSIONID, 浏览器 JS 无法读取(document.cookie 为空),
     因此纯脚本方案要么自行提交登录表单, 要么从浏览器导入该 Cookie。
  3. 登录 POST 成功会 302 到 /welcome; /welcome 本身常被 6666 拦。若自动跟随重定向,
     会将"登录成功"误判为"被限流", 导致反复重复提交登录 —— 故登录 POST 不跟随重定向。
  4. 闸门 /go: 不放行时列表接口**可能**不返回 JSON 而是返回欢迎页 HTML。
     但它是**时有时无**的(取决于服务端会话状态) —— 实测同一账号全新登录,
     13:45 被挡住, 14:13 与 14:14 又直接放行。故改为**按需放行**(见 load_courses):
     先直取列表, 确实被欢迎页挡住才请求 /go(放行后存盘, 供下次跳过)。
  5. 判空余: 列表字段 studentCount 形如 "16/17" (已选/容量)。
  6. urllib 的 CookieJar/opener 不是线程安全的, 故每线程一套 opener,
     Cookie 集中存储于共享 dict 中读写(加锁)。
  7. 选课接口返回**纯文本**(不是 JSON), 取值:
     ok       选课成功
     full     人数已满
     morescore    本学期学分超上限(全局性, 直接放弃)
     moreallscore 三年学分超上限(全局性, 直接放弃)
     stop     该课已停开(剔除出池)
     start    该课已开课(剔除出池)
     selected 你已选过此门课
     busy     时间冲突(剔除出池)
     fail     满员或已选过
     其他/6666 选课失败(重试)

用法:
  python main.py --dry-run        安全预演: 拉列表+构建池不选课, 列表存到 debug/list.json
  python main.py                  正式抢课
  python main.py -t 16            并发 16 路
  python main.py -k 网球          临时覆盖关键字(可多次指定)
  python main.py -c my.toml       指定配置文件
  python main.py -v               每轮重试都打印日志(排查用)
  python main.py --relogin        忽略缓存 Cookie, 强制重新登录
"""

import argparse
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://stmcrp.gzvtc.edu.cn/student/"
BUSY_MARK = '"code":"6666"'
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.49 NetType/WIFI Language/zh_CN"
)

# 从浏览器录制的"真实请求头"(页面里 jQuery $.ajax 发列表时带的), 原样模拟
AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "wx/courseOptionCheck",
}
LOGIN_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE + "wx/login",
}

# 选课接口的业务响应 -> 处置方式
#   ok                 选课成功
#   fatal              全局性失败, 整个任务无意义, 停止
#   drop               该班不可用, 从候选池剔除
#   retry              暂时不可用(如已满), 保留在池中下轮再试
VERDICT = {
    "ok": ("success", "选课成功 ✅"),
    "full": ("retry", "人数已满"),
    "fail": ("retry", "满员或已选过"),
    "selected": ("selected", "你已选过此门课"),
    "busy": ("drop", "时间冲突"),
    "stop": ("drop", "该课已停开"),
    "start": ("drop", "该课已开课"),
    "morescore": ("fatal", "本学期选课学分已超上限"),
    "moreallscore": ("fatal", "三年选课学分已超上限"),
    # select() 重试用尽时返回的哨兵值: 不可当作"选课失败", 需保留在池中下一轮再试
    "timeout": ("retry", "本轮未取得任何响应(限流/超时)"),
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 302。

    登录 POST 成功后服务端会 302 到 /welcome, 而 /welcome 本身常被 6666 拦;
    若跟随重定向, 就会把"登录成功"误判成"被限流", 导致反复重复提交登录。
    改为只看 302 的 Location 判断登录结果。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class McrpClient:
    """学生门户 HTTP 客户端: 自动带 JSESSIONID, 自动穿透 6666 限流。"""

    def __init__(self, cfg):
        self.timeout = cfg["retry"]["timeoutSeconds"]
        self.busy_gap = cfg["retry"]["busyGapMs"] / 1000.0
        conc = cfg.get("concurrency") or {}
        self.threads = max(1, int(conc.get("threads") or 8))
        self.max_attempts = max(self.threads, int(conc.get("maxTotalAttempts") or 2000))
        self.ua = DEFAULT_UA
        self.verbose = bool(cfg["behavior"].get("verbose"))

        self.study = cfg["account"].get("studyNumber") or ""
        self.pwd = cfg["account"].get("password") or ""
        self.force_relogin = bool(cfg["behavior"].get("forceRelogin"))

        sess = cfg.get("session") or {}
        self.cookie_file = self._abs(sess.get("cookieFile") or "cookies.json")
        self.reuse_cookies = sess.get("reuseCookies", True)
        self.cookie_max_age = sess.get("cookieMaxAgeMinutes") or 0
        self.host = urllib.parse.urlsplit(BASE).hostname or ""

        # Cookie 集中存成 dict 由各线程共享(urllib 的 CookieJar 不是线程安全的)
        self.cookies = {}               # name -> {value, domain, path, secure}
        self.cookie_lock = threading.Lock()
        self._tls = threading.local()   # 每线程一套 opener

        self.n_req = 0          # 总请求数
        self.n_busy = 0         # 被 6666 拦截数
        self.n_err = 0          # 超时/网络错误数
        self.n_stale = 0        # 收到的"降级空壳"数(有响应但还没加载出数据)
        self.last_text = ""     # 最近一次请求的原始响应体(用于识别 /go 闸门欢迎页)
        self._stat_lock = threading.Lock()

    @staticmethod
    def _abs(p):
        if os.path.isabs(p):
            return p
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), p)

    # ---------- 会话持久化 ----------

    def load_cookies(self):
        """从 json 恢复 Cookie。返回 (条数, 距保存的分钟数或 None)。"""
        if not self.cookie_file or not os.path.exists(self.cookie_file):
            return 0, None
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            log("[!] cookies.json 解析失败, 视为无缓存")
            return 0, None

        age = None
        try:
            ts = float(data.get("savedAt") or 0)
            if ts > 0:
                age = (time.time() - ts) / 60.0
        except Exception:
            age = None

        n = 0
        with self.cookie_lock:
            for c in data.get("cookies") or []:
                name = c.get("name")
                if not name:
                    continue
                self.cookies[name] = {
                    "value": c.get("value") or "",
                    "domain": c.get("domain") or self.host,
                    "path": c.get("path") or "/",
                    "secure": bool(c.get("secure", True)),
                }
                n += 1
        return n, age

    def save_cookies(self):
        """把当前会话 Cookie 落盘, 供下次直接复用。"""
        try:
            with self.cookie_lock:
                snapshot = [
                    {"name": k, **v} for k, v in self.cookies.items()
                ]
            data = {
                "savedAt": time.time(),
                "savedAtText": time.strftime("%Y-%m-%d %H:%M:%S"),
                "host": self.host,
                "cookies": snapshot,
            }
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log(f"[OK] 会话已缓存到 {os.path.basename(self.cookie_file)}"
                f" (共 {len(snapshot)} 条)")
            return True
        except Exception as e:
            log(f"[!] 保存 Cookie 失败: {e}")
            return False

    def ensure_session(self):
        """有可用缓存就直接用; 文件为空 / 超龄 / 失效才重新登录。"""
        if self.reuse_cookies and not self.force_relogin:
            n, age = self.load_cookies()
            if n:
                if self.cookie_max_age and age is not None and age > self.cookie_max_age:
                    log(f"[!] 缓存会话已 {age:.0f} 分钟(上限 {self.cookie_max_age}), 重新登录")
                else:
                    when = f", 保存于 {age:.0f} 分钟前" if age is not None else ""
                    log(f"[OK] 复用缓存会话({n} 个 Cookie{when}), 跳过登录阶段")
                    return True
        if not self.pwd:
            log("[X] account.password 为空, 无法登录")
            return False
        if not self.login():
            return False
        self.save_cookies()
        return True

    # ---------- 底层 ----------

    def _thread_openers(self):
        """每线程独立的 opener + CookieJar, 避免并发写坏同一个 jar。"""
        pair = getattr(self._tls, "pair", None)
        if pair is None:
            jar = http.cookiejar.CookieJar()
            follow = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            raw = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
            pair = (follow, raw, jar)
            self._tls.pair = pair
        return pair

    def _cookies_into(self, jar):
        """请求前: 把共享 Cookie 灌进本线程的 jar。"""
        with self.cookie_lock:
            items = list(self.cookies.items())
        for name, c in items:
            try:
                jar.set_cookie(http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=c["value"],
                    port=None,
                    port_specified=False,
                    domain=c.get("domain") or self.host,
                    domain_specified=True,
                    domain_initial_dot=False,
                    path=c.get("path") or "/",
                    path_specified=True,
                    secure=bool(c.get("secure", True)),
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                ))
            except Exception:
                pass

    def _cookies_from(self, jar):
        """响应后: 把服务端新发的 Cookie 回写共享 dict(如登录拿到的 JSESSIONID)。"""
        try:
            found = list(jar)
        except Exception:
            return
        if not found:
            return
        with self.cookie_lock:
            for c in found:
                self.cookies[c.name] = {
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "secure": bool(c.secure),
                }

    def _open(self, url, data=None, follow=True, extra=None):
        follow_op, raw_op, jar = self._thread_openers()
        self._cookies_into(jar)

        hdrs = {"User-Agent": self.ua}
        if extra:
            hdrs.update(extra)
        req = urllib.request.Request(url, data=data, headers=hdrs)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self._stat_lock:
            self.n_req += 1

        opener = follow_op if follow else raw_op
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                self._cookies_from(jar)
                return resp.getcode(), resp.headers, text
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            self._cookies_from(jar)
            return e.code, e.headers, text
        except Exception as e:                      # 超时 / 连接重置等
            return None, {}, f"__ERR__ {type(e).__name__}"

    # list 接口被 /go 闸门挡住时, 返回的是"欢迎页"HTML —— 用这些特征词识别
    GATE_MARKERS = ("进入学生门户", "已注册", "欢迎")

    # 非选课时段, 服务端会把整个选课页换成一张极简提示页, 上面就写着这句话
    CLOSED_MARK = "当前时间不可选课"

    @staticmethod
    def page_state(text):
        """给"选课页"的响应分类, 判断现在能不能选课。

        ⚠️ 实测(2026-09-28): 「当前时间不可选课」是**服务端渲染**的 ——
        非选课时段 GET `wx/courseOptionCheck` 返回的是一张 **~1.2KB 的极简页**
        (只有标题 + 一个 info 图标 + 这句话), 里面既没 Vue 也没列表脚本;
        可选课时返回的才是完整的选课页(带 `pulldownRefresh` / `courseList`)。
        因此该判断可靠。

        返回 "open" / "closed" / "login"(会话过期) / None(判断不了, 例如被 6666 拦)。
        """
        if not text or text.startswith("__ERR__"):
            return None
        if McrpClient.CLOSED_MARK in text:
            return "closed"
        if "pulldownRefresh" in text or "courseList" in text:
            return "open"
        if "login-form" in text:
            return "login"
        return None

    @staticmethod
    def _is_gate_page(text):
        """判断 list 接口是不是被 /go 闸门挡住了(返回欢迎页 HTML 而不是 JSON)。

        实测(2026-09-28, 用全新会话验证):
          - **全新登录**的会话直打 list -> 返回欢迎页 HTML
            (Content-Type=text/html;charset=UTF-8, 含 "进入学生门户/已注册/欢迎")
          - **复用已放行过闸门**的缓存会话直打 list -> 直接返回 JSON, 无需 /go
        这说明闸门状态存在**服务端会话**里, 不是每个进程都要重走一遍。
        """
        if not text or text.startswith("__ERR__"):
            return False
        head = text[:400].lower()
        if "<html" not in head and "<!doctype" not in head:
            return False
        return any(m in text[:6000] for m in McrpClient.GATE_MARKERS)

    @staticmethod
    def _is_business(text, code):
        """判断响应是不是"业务响应"(既不是 6666 限流, 也不是网络错误)。

        ⚠️ 关键问题(实测 2026-09-28 遇到, 症状=「登录始终不成功」):
        登录成功是 **302 重定向, 其响应体常常是空的**。
        仅按 "bool(text)" 判定时, 成功的 302 会被判为"未取得响应"而无限重试。
        故 3xx 重定向即使空体也必须算业务响应。
        """
        if text.startswith("__ERR__"):
            return False
        if BUSY_MARK in text:
            return False
        if 300 <= code < 400:
            return True
        return bool(text)

    @staticmethod
    def list_ready(text):
        """列表"真的加载出来了"的判据。

        ⚠️ 实测: 服务端高峰期会返回两种**降级响应**, 都必须当作"还没加载出来"继续重试:
          1) 空壳:  {"pageNum":1,"pageSize":0,"size":0,"total":0,"list":[]}
          2) 占位行: list 里只有 1 条 courseName 为 null 的旧数据
             (如 {"courseName":null,"currentSemester":"2020-2021学年第一学期",...})
        它们 HTTP 200、不含 6666, 但都不是本次要的数据。
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        rows = data.get("list")
        if not rows:
            return False
        # 至少要有一条"像真课程"的记录(有 courseName)才算加载成功
        return any((r or {}).get("courseName") for r in rows)

    def _fan_out(self, url, data, follow, n, extra=None):
        """并发 n 路, 每路各发一次请求, 收齐后一起返回。"""
        out = [None] * n
        lock = threading.Lock()

        def one(i):
            try:
                code, headers, text = self._open(url, data, follow=follow, extra=extra)
            except Exception:
                return
            with lock:
                out[i] = (text, headers, code)

        workers = [threading.Thread(target=one, args=(i,), daemon=True)
                   for i in range(n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        good = [o for o in out if o]
        with self._stat_lock:
            for text, _, _ in good:
                if BUSY_MARK in text:
                    self.n_busy += 1
                elif text.startswith("__ERR__"):
                    self.n_err += 1
        return good

    @staticmethod
    def _pick(hits, prefer):
        """在本轮所有业务响应里挑一个: 优先满足 prefer, 否则取第一个。"""
        if prefer:
            for h in hits:
                if prefer(h[0]):
                    return h
        return hits[0] if hits else None

    def request(self, path, data=None, label="", follow=True, prefer=None, extra=None,
                accept=None, rounds=None):
        """多线程并发请求: 每轮同时打 threads 路, 一轮内收齐所有响应再决策。

        - 本轮只要有任一"合格响应", 就直接用它作为结果(不再等下一轮)
        - prefer: 在同一轮多个合格响应里挑最优(如选课优先认 'ok')
        - accept: 判断"这响应算不算数"。列表接口用它把**降级空壳**判为不合格,
          于是会像 6666 一样继续重试, 直到真正加载出数据。
        - rounds: 覆盖默认轮数(复核等场景用少量轮数即可, 不占满预算)
        - 整轮都不合格时立刻开下一轮, 不串行等待

        返回 (text, headers, status, attempts, ok)
        """
        url = path if path.startswith("http") else BASE + path
        name = label or path
        n = self.threads
        total_rounds = rounds if (rounds and rounds > 0) else max(1, self.max_attempts // n)
        t0 = time.time()
        last_reject = None

        for r in range(1, total_rounds + 1):
            hits = self._fan_out(url, data, follow, n, extra)
            biz = [h for h in hits if self._is_business(h[0], h[2])]
            good = [h for h in biz if accept is None or accept(h[0])]

            if good:
                chosen = self._pick(good, prefer)
                if chosen is not None:
                    if r > 1:
                        log(f"      {name}: 第 {r} 轮命中 "
                            f"({n} 路并发, 共 {r * n} 次请求, {time.time() - t0:.1f}s)")
                    self.last_text = chosen[0]
                    return chosen[0], chosen[1], chosen[2], r * n, True

            if biz:
                last_reject = biz[0]
                with self._stat_lock:
                    self.n_stale += len(biz)
                if self.verbose or r % 3 == 0:
                    log(f"      {name}: 第 {r} 轮收到降级空壳"
                        f"({biz[0][0][:55]}...), 继续重试")
            elif self.verbose or r % 3 == 0:
                why = {}
                for text, _, _ in hits:
                    if BUSY_MARK in text:
                        k = "6666"
                    elif text.startswith("__ERR__"):
                        k = "网络错误"
                    elif not text:
                        k = "空响应"
                    else:
                        k = "其他"
                    why[k] = why.get(k, 0) + 1
                log(f"      {name}: 第 {r} 轮 {n} 路无有效响应 ("
                    + ", ".join(f"{k}×{v}" for k, v in why.items()) + "), 继续")
            time.sleep(self.busy_gap)

        if last_reject is not None:
            log(f"[!] {name}: 已重试 {total_rounds * n} 次仍未收到就绪数据, 返回最后一次响应")
            self.last_text = last_reject[0]
            return last_reject[0], last_reject[1], last_reject[2], total_rounds * n, True
        self.last_text = ""
        return "", {}, None, total_rounds * n, False

    def get(self, path, params=None, **kw):
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        return self.request(path, **kw)

    def post(self, path, form, **kw):
        data = urllib.parse.urlencode(form).encode()
        return self.request(path, data=data, **kw)

    # ---------- 业务 ----------

    def login(self):
        """直接 POST 登录表单换取 JSESSIONID —— 不预先 GET 登录页。"""
        if not self.study or not self.pwd:
            log("[X] 配置缺少 account.studyNumber / account.password")
            return False

        # 表单字段固定为 studyNumber / password
        # 不跟随重定向, 用 302 的 Location 判定结果
        text, headers, code, tries, ok = self.post(
            "wx/login",
            {"studyNumber": self.study, "password": self.pwd},
            label="登录",
            follow=False,
            extra=LOGIN_HEADERS,
        )
        if not ok:
            log("[X] 登录请求始终被限流/失败")
            return False

        loc = headers.get("Location") or ""
        if code in (301, 302, 303, 307) and ("welcome" in loc or "index" in loc):
            log(f"[OK] 登录成功 (HTTP {code} -> {loc})")
            return True
        if "进入学生门户" in text or "已注册" in text:
            log("[OK] 登录成功 (HTTP 200 直接返回门户页)")
            return True
        log(f"[X] 登录失败 (HTTP {code}, Location={loc!r}, 响应={text[:100]!r})")
        return False

    def pass_gate(self):
        """固定动作: 放行"欢迎页"闸门。

        实测: 不做这一步时, 课程列表接口不会返回 JSON, 而是返回欢迎页 HTML。
        """
        text, headers, code, tries, ok = self.get("wx/go", label="放行闸门(/go)")
        if not ok:
            log("[X] /go 始终被限流/失败")
            return False
        log(f"[OK] 闸门已放行 (第 {tries} 次, HTTP {code})")
        return True

    def selection_state(self, rounds=3):
        """查当前能不能选课 —— 返回 "open" / "closed" / "login" / None(没拿到有效响应)。

        只需 GET 一次选课页即可判定(见 page_state 的说明),
        比"看列表为什么是空的"可靠得多。
        """
        text, _, _, _, ok = self.get(
            "wx/courseOptionCheck", label="探测选课开放状态", rounds=rounds)
        if not ok:
            return None
        return self.page_state(text)

    def fetch_list(self, semester, belonging="", rounds=None, quiet=False):
        """直接 GET 课程列表接口。

        返回 (行列表|None, 会话是否失效)。
        降级空壳由 accept=list_ready 拦下并继续重试, 不在这里判空。
        """
        text, headers, _, _, ok = self.get(
            "wx/courseOptionCheck/list",
            {
                "pageNum": 1,
                "pageSize": 200,
                "currentSemester": semester,
                "belonging": belonging,
            },
            label="复核列表" if quiet else "课程列表",
            extra=AJAX_HEADERS,
            accept=self.list_ready,
            rounds=rounds,
        )
        if not ok:
            return None, False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            expired = ("login-form" in text
                       or "login" in (headers.get("Location") or ""))
            log(f"[!] 列表响应非 JSON, 片段={text[:120]!r}")
            return None, expired
        return (data.get("list") or []), False

    def confirm_selected(self, course, semester, belonging=""):
        """选课成功后的复核(尽力而为): 重新拉一次列表, 对比该教学班人数。

        抢课前 16/17 → 成功后应变成 17/17。列表获取失败(限流/降级)时跳过, 不影响结论。
        """
        before = course.get("studentCount")
        cid = course.get("id")
        rows = self.fetch_list(semester, belonging, rounds=8, quiet=True)
        if rows is None:
            log("[i] 复核: 列表暂时获取失败(限流/降级), 跳过复核, 请自行在选课页确认")
            return
        hit = next((r for r in rows if r.get("id") == cid), None)
        if hit is None:
            log("[i] 复核: 该教学班已不在列表里(可能已满/已停开), 请自行在选课页确认")
            return
        after = hit.get("studentCount")
        flag = "✅" if after != before else "⚠️"
        log(f"{flag} 复核: {hit.get('courseName')} 人数 {before} -> {after}"
            + ("" if after != before else "  (人数未变, 建议前往选课页确认)"))

    def load_courses(self, semester, belonging="", loud=True):
        """拉课程列表 —— **按需**放行 /go 闸门。

        实测(2026-09-28 两组对照, 结论: 闸门**时有时无**, 不可假定):
          - 13:45 全新登录会话直打 list -> 返回欢迎页 HTML, 必须过 /go
          - 14:13 / 14:14 全新登录会话直打 list -> 直接返回 JSON, 不用过 /go
        因此 /go 既不可无条件跳转、也不应每次空跑: 先直取列表, 确实被欢迎页挡住时才请求它。

        返回课程列表; 获取失败返回 None。
        """
        courses, expired = self.fetch_list(semester, belonging)
        if courses is not None:
            if loud:
                log("[OK] 列表直接可用, 无需 /go 闸门")
            return courses

        # 情况 A: 被欢迎页挡住 -> 放行闸门后重取
        if self._is_gate_page(self.last_text):
            log("[i] 列表被欢迎页挡住 → 放行 /go 闸门后重取")
            if not self.pass_gate():
                return None
            # 闸门状态在服务端会话里; 存盘后下次运行即可跳过这一步
            self.save_cookies()
            courses, _ = self.fetch_list(semester, belonging)
            return courses

        # 情况 B: 服务端明确判会话失效 -> 重新登录(新会话必然要再过闸门)
        if expired:
            log("[!] 会话已失效, 重新登录")
            if not self.login():
                return None
            self.save_cookies()
            if not self.pass_gate():
                return None
            self.save_cookies()
            courses, _ = self.fetch_list(semester, belonging)
            return courses

        return courses

    def select(self, course_id):
        """并发发起选课。

        同一轮里如果有任一路取得 'ok', 就判定为 'ok'(避免已取得的成功结果被其他线程的
        'selected'/'fail' 覆盖); 整轮没有 'ok' 则取本轮第一个业务响应返回给上层决策。
        """
        text, _, _, tries, ok = self.get(
            "wx/courseOptionCheck/check",
            {"id": course_id},
            label=f"选课 id={course_id}",
            prefer=lambda t: t == "ok",
            extra=AJAX_HEADERS,
        )
        if not ok:
            return "timeout", tries
        return text.strip(), tries


# ---------- 候选筛选 ----------

def vacancy(student_count):
    """'16/17' -> (16, 17); 解析失败返回 None。"""
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*$", str(student_count or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def match_keyword(course, keywords):
    """返回该课命中的关键字; 没命中返回 None。关键字为空表示"不限"。"""
    name = course.get("courseName") or ""
    for k in keywords:
        if k and k in name:
            return k
    return None


def keyword_stats(courses, keywords, only_with_vacancy):
    """统计每个关键字: 匹配到多少门 / 其中多少门真的进了池。

    返回 {关键字: (匹配数, 入池数)}。
    只有 only_with_vacancy=True 时满员的班才会被排除, 否则匹配到多少就入池多少。
    """
    stat = {}
    for c in courses:
        if keywords:
            kw = match_keyword(c, keywords)
            if kw is None:
                continue
        else:
            kw = ""
        matched, pooled = stat.get(kw, (0, 0))
        matched += 1
        vs = vacancy(c.get("studentCount"))
        free = (vs[1] - vs[0]) if vs else 0
        if not (only_with_vacancy and vs and free <= 0):
            pooled += 1
        stat[kw] = (matched, pooled)
    return stat


def build_pool(courses, keywords, only_with_vacancy):
    """从课程列表构建**选课池**: 所有匹配**任一**关键字的课都在池里。

    多个关键字时按关键字**轮转交错**排列 ——
    如 [尤克里里, 瑜伽, 尤克里里, 瑜伽, ...], 而不是把某一个关键字的班全排前面。
    这样池内逐个尝试时, 两个目标课程的抢课机会是均等的。
    同一关键字内部: 空位多的排前面, 满员的排最后(满员也可能中途有人退课)。
    only_with_vacancy=True 时才把满员的班彻底排除。

    返回 [(命中的关键字, 课程)]; keywords 为空时关键字为 ""。
    """
    buckets = {}
    order = []          # 关键字首次出现的顺序, 决定轮转次序
    for c in courses:
        if keywords:
            kw = match_keyword(c, keywords)
            if kw is None:
                continue
        else:
            kw = ""
        vs = vacancy(c.get("studentCount"))
        free = (vs[1] - vs[0]) if vs else 0
        if only_with_vacancy and vs and free <= 0:
            continue
        if kw not in buckets:
            buckets[kw] = []
            order.append(kw)
        buckets[kw].append((free, c))

    for kw in order:
        buckets[kw].sort(key=lambda x: -x[0])

    # 轮转交错
    pool = []
    idx = 0
    while True:
        added = False
        for kw in order:
            if idx < len(buckets[kw]):
                pool.append((kw, buckets[kw][idx][1]))
                added = True
        if not added:
            break
        idx += 1
    return pool


def describe(c):
    return (
        f"{c.get('courseName')} [{c.get('teachingClassName')}] "
        f"{c.get('studentCount')} | {c.get('teacherNames')} | {c.get('courseMemo')}"
    )


# ---------- 主流程 ----------

def all_achieved(targets, achieved):
    """退出条件: targets 里的每个类型都已在 achieved 里才算全部抢到。"""
    return all(t in achieved for t in targets)


def _summary(cli, cycles):
    log(f"[=] 统计 | 请求 {cli.n_req} 次 | 6666 拦截 {cli.n_busy} 次 | "
        f"降级空壳 {cli.n_stale} 次 | 网络错误 {cli.n_err} 次 | 池循环 {cycles} 轮")


def should_refresh(cycle, every=1, enabled=True):
    """第 cycle 轮要不要重新拉一次课程列表(刷新余量)。

    cycle 从 1 开始; 第 1 轮的列表是启动时刚拉的, 不再重复拉。
    every=1 → 第 2 轮起每轮都刷(默认); every=5 → 第 5/10/15… 轮刷;
    enabled=False → 永不刷。

    （列表接口同样受 6666 拦截, 池子较大时无需每轮刷新 —— 本开关用于控制该节奏。）
    """
    if not enabled or cycle <= 1:
        return False
    return cycle % max(1, int(every)) == 0


def next_refresh_cycle(cycle, every=1, enabled=True):
    """下一次会重拉列表的轮次; 永不刷新则返回 None(日志用它预告)。"""
    if not enabled:
        return None
    every = max(1, int(every))
    nxt = cycle + 1
    while nxt % every != 0:
        nxt += 1
    return nxt


def run(cfg, args):
    keywords = args.keyword or cfg["target"]["keywords"] or []
    belonging = cfg["target"]["belonging"] or ""
    only_vac = bool(cfg["target"]["onlyWithVacancy"])
    dry = args.dry_run or cfg["behavior"]["dryRun"]
    selected_ok = bool(cfg["behavior"]["treatSelectedAsSuccess"])
    require_all = bool(cfg["behavior"].get("requireAllKeywords", True))
    list_gap = cfg["retry"]["listIntervalMs"] / 1000.0
    refresh_list = bool(cfg["retry"].get("refreshList", True))
    refresh_every = max(1, int(cfg["retry"].get("refreshListEveryCycles") or 1))
    minutes = cfg["retry"]["maxRunMinutes"]
    deadline = time.time() + minutes * 60 if minutes > 0 else None

    def overtime():
        return deadline is not None and time.time() > deadline

    # ---- 非选课时段: 不报错退出, 而是按间隔探测, 等待选课开放 ----
    wait_when_closed = bool(cfg["behavior"].get("waitWhenClosed", True))
    closed_gap = max(1, int(cfg["retry"].get("closedWaitSeconds") or 30))
    probe = {"at": 0.0, "open": False}

    def wait_open():
        """确认选课已开放; 不可选课时轮询等待。

        返回 True = 可以继续抢课; False = 应结束运行。
        ⚠️ behavior.waitWhenClosed=false 时直接返回 True(保持旧行为)。
        ⚠️ 试运行(--dry-run)不等待: 仅快速查看池子, 阻塞在此无意义。
        """
        if not wait_when_closed:
            return True
        waited = 0
        while True:
            # 刚探过是开放的 → 短时间内不重复探测(避免每轮空池都额外发起请求)
            if probe["open"] and time.time() - probe["at"] < closed_gap:
                return True
            state = cli.selection_state()
            probe["at"] = time.time()
            probe["open"] = (state == "open")
            if state == "open":
                if waited:
                    log(f"[OK] 选课已开放(等了 {waited // 60} 分 {waited % 60} 秒), 开始抢课")
                return True
            if dry:
                log(f"[=] 现在不能选课(状态={state}), 试运行不等待, 直接结束")
                return False
            if state == "login":
                # 等待可能持续数小时, JSESSIONID 会过期 → 探测到登录页就补一次登录
                log("[!] 会话已失效(探测到登录页), 重新登录后继续等")
                if cli.login():
                    cli.save_cookies()
            elif state == "closed":
                log(f"[=] 当前时间不可选课 —— {closed_gap} 秒后再探测 (Ctrl+C 退出)")
            else:
                log(f"[!] 没拿到有效的选课页响应(可能被 6666 限流), {closed_gap} 秒后重试")
            if overtime():
                log("[=] 等到时限仍未开放, 结束")
                return False
            time.sleep(closed_gap)
            waited += closed_gap

    if not cfg["account"].get("studyNumber"):
        log("[X] 配置里 account.studyNumber 为空")
        return 2

    cli = McrpClient(cfg)

    log("=" * 70)
    log(f"抢课目标: 关键字={keywords or '(不限)'}  归属={belonging or '全部'}  "
        f"只抢空位={only_vac}  "
        f"退出条件={'每个类型都抢到才退出' if require_all else '抢到一门即停'}  "
        f"不可选课时={'等待开放' if wait_when_closed else '不等待'}  "
        f"{'[试运行]' if dry else ''}")
    if keywords:
        log(f"共 {len(keywords)} 个类型: " + " / ".join(keywords))
    log("=" * 70)

    # ---- 1) 会话: 有缓存就用, 没有/过期/失效才登录 ----
    log("[▶] 建立会话")
    if not cli.ensure_session():
        return 1

    # ---- 2) 等选课开放: 非选课时段轮询等待, 既不空转也不报错退出 ----
    log("[▶] 检查选课时段")
    if not wait_open():
        return 1

    # ---- 3) 拉课程列表(参数固定) ----
    # ⚠️ /go 闸门已改为**按需**放行(见 load_courses): 缓存会话若已放行过就直接取列表,
    #    只有真被欢迎页挡住了才走 /go —— 省去这一步的 6666 消耗。
    log("[▶] 拉取课程列表")
    semester = cfg["target"]["semester"] or ""
    if not semester:
        log("[X] 配置里 target.semester 为空, 请填写学期字符串")
        return 2
    log(f"[OK] 学期 = {semester} (取自配置)")
    courses = cli.load_courses(semester, belonging)
    if courses is None:
        log("[X] 课程列表获取失败, 退出")
        return 1
    if dry:
        path = save_list(courses, semester)
        log(f"[i] 列表已存盘: {path} ({len(courses)} 门)")

    dead = set()        # 已判定不可用的班(时间冲突/已停开/已开课), 剔除出池; 重拉列表后清空
    achieved = {}       # 类型(关键字) -> 已抢到的课程; 每个类型只要一门
    cycles = 0

    # 每个关键字算一个"类型"; 关键字为空时只有一种类型
    targets = list(keywords) if keywords else [""]

    def pending():
        return [t for t in targets if t not in achieved]

    def log_progress():
        log("    目标进度 {}/{}: ".format(len(achieved), len(targets))
            + "  ".join(f"{t or '(不限)'}{'✅' if t in achieved else '⏳'}"
                        for t in targets))

    def finish(reason):
        """收尾: 打印每个类型的达成情况。"""
        log("=" * 70)
        log(reason)
        for t in targets:
            cc = achieved.get(t)
            log(f"     [{'✅' if cc else '❌'}] {t or '(不限)'} "
                + (describe(cc) if cc else "(未抢到)"))
        log("=" * 70)
        _summary(cli, cycles)

    log("[▶] 抢课")
    while True:
        if overtime():
            log(f"[=] 到时限({minutes} 分钟), 结束")
            break

        cycles += 1

        # 刷新余量: 重新拉取列表以获取最新空位（由 refreshList / refreshListEveryCycles 控制）
        if should_refresh(cycles, refresh_every, refresh_list):
            log(f"    [i] 刷新课程列表(每 {refresh_every} 轮一次)")
            fresh = cli.load_courses(semester, belonging, loud=False)
            if fresh is None:
                log(f"    [!] 刷新失败(限流/降级), 沿用上一份 {len(courses)} 门")
            else:
                courses = fresh
                log(f"    [OK] 列表已刷新: {len(courses)} 门")
                # 新列表反映当前状态 -> 上一份的废弃记录作废, 重新评估
                if dead:
                    log(f"    [i] 清空废弃记录({len(dead)} 个班重新入池评估)")
                    dead.clear()

        # 池内: 只保留"尚未抢到的类型"的班(已抢到的类型不再重复抢)
        pool = [(kw, c) for kw, c in build_pool(courses, keywords, only_vac)
                if kw not in achieved and c.get("id") not in dead]

        log(f"===== 第 {cycles} 轮池循环 | 池内 {len(pool)} 门 "
            f"(列表 {len(courses)} 门, 已废弃 {len(dead)} 个) =====")
        if keywords:
            stat = keyword_stats(courses, keywords, only_vac)
            log("    关键字匹配: " + ", ".join(
                f"{kw}={p}/{m} 门" + ("" if p == m else f"(满员排除 {m - p})")
                for kw, (m, p) in stat.items()))
            inpool = {}
            for kw, _ in pool:
                inpool[kw] = inpool.get(kw, 0) + 1
            log("    入池分布(按关键字轮转交错): "
                + (", ".join(f"{k}×{v}" for k, v in inpool.items()) or "(空)"))
            for t in pending():         # 待抢的关键字一门都没匹配到 -> 明确告警
                if stat.get(t, (0, 0))[0] == 0:
                    log(f"    ⚠️ 关键字『{t}』本轮列表里没匹配到任何班级(等下一轮再看)")
        log_progress()

        if not pool:
            if not pending():
                finish(f"🎉 全部 {len(targets)} 个类型都已抢到, 任务完成")
                return 0
            log(f"[{cycles}] 池内无可抢的班(列表 {len(courses)} 门, 已废弃 {len(dead)} 个), 稍后重试")
            if dry:
                return 0
            # 池空也可能是"选课窗口关了"(服务端此时只返回极简提示页 + 空列表) → 轮询等待重新开放
            if not wait_open():
                return 1
            time.sleep(list_gap)
            continue

        tally = {}          # 本轮各类响应计数, 轮末一行汇总
        for i, (kw, c) in enumerate(pool, 1):
            if overtime():
                break
            tag = f"[{i}/{len(pool)}]" + (f"[{kw}]" if kw else "")

            if dry:
                log(f"  {tag} {describe(c)}")
                continue

            cid = c.get("id")
            resp, tries = cli.select(cid)
            verdict, desc = VERDICT.get(resp, ("retry", f"未知响应 {resp!r}"))

            # ---------- 选课成功: 该类型达成 ----------
            if verdict == "success":
                achieved[kw] = c
                log("=" * 70)
                log(f"🎉 选课成功! {tag} {describe(c)}")
                log(f"   类型『{kw or '(不限)'}』已达成 ({len(achieved)}/{len(targets)})")
                log("=" * 70)
                cli.confirm_selected(c, semester, belonging)    # 尽力复核人数变化
                log(f"[i] 如需退课: GET /student/wx/courseOptionCheck/noCheck?id={cid}")
                if (not require_all) or all_achieved(targets, achieved):
                    if require_all:
                        finish(f"🎉 任务完成: {len(achieved)}/{len(targets)} 个类型都已抢到")
                    else:
                        finish(f"🎉 已抢到一门({len(achieved)}/{len(targets)}), "
                               "requireAllKeywords=false 故按设定退出")
                    return 0
                log(f"    还剩 {len(pending())} 个类型待抢({', '.join(pending())}), 继续...")
                continue

            # ---------- 服务端说"你已选过此门课" ----------
            if verdict == "selected":
                if selected_ok:
                    achieved[kw] = c
                    log(f"  {tag} -> {desc}, 视为类型『{kw or '(不限)'}』已达成 "
                        f"({len(achieved)}/{len(targets)})")
                    if (not require_all) or all_achieved(targets, achieved):
                        if require_all:
                            finish(f"🎉 任务完成: {len(achieved)}/{len(targets)} 个类型都已达成"
                                   "(其中部分为『已选过』)")
                        else:
                            finish(f"🎉 已达成一门({len(achieved)}/{len(targets)}), "
                                   "requireAllKeywords=false 故按设定退出")
                        return 0
                    continue
                dead.add(cid)
                log(f"  {tag} -> {desc}, 剔除出池(不再试该班), 下一个")
                continue

            # ---------- 全局性失败: 继续抢没有意义 ----------
            if verdict == "fatal":
                log(f"[X] {desc}, 停止本次抢课")
                finish(f"[X] 因『{desc}』提前结束")
                return 3

            # ---------- 该班永久不可用: 剔除出池 ----------
            if verdict == "drop":
                dead.add(cid)
                log(f"  {tag} -> {desc}, 剔除出池, 下一个")
                continue

            # ---------- 其余(满员/满员或已选过/超时/未知码): 保留在池中, 立即跳转下一个 ----------
            tally[desc] = tally.get(desc, 0) + 1
            log(f"  {tag} -> {desc}, 保留在池中等下一轮, 下一个")

        if dry:
            log(f"[试运行] 池内 {len(pool)} 门, 未发送任何选课请求")
            return 0

        # 一轮收尾: 汇总本轮结果与下次刷新轮次
        nxt = next_refresh_cycle(cycles, refresh_every, refresh_list)
        log(f"--- 第 {cycles} 轮走完: "
            + ("、".join(f"{k}×{v}" for k, v in tally.items()) or "无保留在池中的班")
            + (f" | 下次刷新在第 {nxt} 轮" if nxt else " | 不刷新列表")
            + " ---")
        time.sleep(list_gap)

    finish(f"[=] 未能全部抢到, 已结束: 已达成 {len(achieved)}/{len(targets)} 个类型")
    return 0


def _fill_defaults(cfg):
    """补齐缺省字段, 避免 KeyError(配置文件里没写的键用代码默认值)。"""
    cfg.setdefault("account", {})
    cfg.setdefault("target", {})
    cfg["target"].setdefault("keywords", [])
    cfg["target"].setdefault("belonging", "")
    cfg["target"].setdefault("semester", "")
    cfg["target"].setdefault("onlyWithVacancy", False)
    cfg.setdefault("session", {})
    cfg["session"].setdefault("cookieFile", "cookies.json")
    cfg["session"].setdefault("reuseCookies", True)
    cfg["session"].setdefault("cookieMaxAgeMinutes", 0)
    cfg.setdefault("retry", {})
    cfg["retry"].setdefault("busyGapMs", 80)
    cfg["retry"].setdefault("listIntervalMs", 1500)
    cfg["retry"].setdefault("refreshList", True)
    cfg["retry"].setdefault("refreshListEveryCycles", 1)
    cfg["retry"].setdefault("timeoutSeconds", 6)
    cfg["retry"].setdefault("maxRunMinutes", 0)
    cfg["retry"].setdefault("closedWaitSeconds", 30)
    cfg.setdefault("concurrency", {})
    cfg["concurrency"].setdefault("threads", 8)
    cfg["concurrency"].setdefault("maxTotalAttempts", 2000)
    cfg.setdefault("behavior", {})
    cfg["behavior"].setdefault("requireAllKeywords", True)
    cfg["behavior"].setdefault("treatSelectedAsSuccess", True)
    cfg["behavior"].setdefault("dryRun", False)
    cfg["behavior"].setdefault("verbose", False)
    cfg["behavior"].setdefault("forceRelogin", False)
    cfg["behavior"].setdefault("waitWhenClosed", True)
    return cfg


def here_path(name):
    """把相对路径解析成绝对路径(基准 = 脚本所在目录)。"""
    if os.path.isabs(name):
        return name
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def save_list(courses, semester, name="debug/list.json"):
    """把拉到的课程列表原样存成 JSON(附学期/时间/条数), 便于离线查看。返回落盘路径。"""
    path = here_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "semester": semester,
            "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(courses),
            "list": courses,
        }, f, ensure_ascii=False, indent=2)
    return path


def load_config(name="config.toml"):
    """读 TOML 配置并补齐缺省字段。

    `tomllib` 是 Python 3.11+ 标准库, 所以脚本仍然是零依赖。
    """
    with open(here_path(name), "rb") as f:
        cfg = tomllib.load(f)
    return _fill_defaults(cfg)


def main():
    ap = argparse.ArgumentParser(description="广州工程技术职业学院网上选课抢课脚本")
    ap.add_argument("-c", "--config", default="config.toml", help="配置文件路径, 默认 config.toml")
    ap.add_argument("-k", "--keyword", action="append", help="课程关键字(可多次), 覆盖配置")
    ap.add_argument("--dry-run", action="store_true",
                    help="安全预演: 拉列表+构建池不选课, 并把列表存到 debug/list.json")
    ap.add_argument("--relogin", action="store_true", help="忽略缓存 Cookie, 强制重新登录")
    ap.add_argument("-t", "--threads", type=int, help="并发路数, 覆盖配置")
    ap.add_argument("-v", "--verbose", action="store_true", help="每轮重试都打印日志")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        log(f"[X] 找不到配置文件: {here_path(args.config)}")
        log(f"    复制一份模板再改: config.example.toml -> {args.config}")
        return 2
    except Exception as e:
        log(f"[X] 配置文件解析失败({type(e).__name__}): {e}")
        return 2

    # 命令行开关优先级高于配置文件
    if args.threads:
        cfg["concurrency"]["threads"] = args.threads
    if args.verbose:
        cfg["behavior"]["verbose"] = True
    if args.relogin:
        cfg["behavior"]["forceRelogin"] = True

    try:
        return run(cfg, args)
    except KeyboardInterrupt:
        log("已手动中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
