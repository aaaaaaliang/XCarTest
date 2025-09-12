#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBOX 休眠/唤醒循环验证（Windows 主机 + 并行 Ping + SSH 预算120s + PASS/FAIL 报告）
-----------------------------------------------------------------------------
本版按最新口径实现要点：
1) Host 侧使用 Windows 专用 ping 判定（解析 ping 回显中的 TTL / Reply from / 来自…的回复），避免误判。
2) 外部 Ping、主模组内 Ping、副模组内 Ping 均“并行执行”，不要求顺序；最终结果只关心是否能打通。
3) SSH 连接预算：在“电流 > 400mA 且保持”达标后，再给 120s 的连接预算；超时视为未连上（该模组内所有 IP 统一 FAIL）。
4) 控制台输出分区块（外部/主/副 + 休眠判定）；每 IP 展示 PASS/FAIL、尝试次数、首次打通时间(first_ok_s)。
5) CSV 摘要新增 host_pass_map/main_pass_map/backup_pass_map 三列（JSON）；notes 保留 first_ok_map/计数器/错误等。
6) 休眠判定给出 stop→last_tx_seen_s 与 sleep_reached_s 双指标；可选 OUT_TX 日志节流开关避免刷屏。
依赖：
  pip install python-can paramiko
你的库中已提供：
  from car_test import OwonPSU, CanBus, SSHClient
-------------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import csv
import json
import time
import random
import threading
import subprocess
import re
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

import can  # 仅用于构造 Message；底层总线由 car_test.CanBus 打开
from car_test import OwonPSU, CanBus, SSHClient

# ========================= 配置区（按现场改） =========================
# —— 首次上电策略 ——
FIRST_BOOT_SEND_WAKE: bool = True              # True=上电立刻发0x795保持唤醒；False=不发，让TBOX自管
FIRST_BOOT_STOP_WAKE_AFTER_PINGS: bool = True  # 首次上电如果发了0x795，做完ping后是否立即停发

# —— 电源/电流判据（可选） ——
USE_PSU: bool = True
PSU_COM: str = "COM3"
PSU_BAUD: int = 115200
PSU_VOLT_SET: float = 12.0
PSU_CURR_LIMIT_A: float = 3.0
PSU_SET_OUTPUT: bool = True
PSU_LEAVE_ON_AT_EXIT: bool = True
POWER_WAKE_THRESHOLD_MA: float = 400.0   # 唤醒电流达标阈值（mA）
POWER_HOLD_MS: int = 600                 # 电流连续达标保持时间（ms）
POWER_TIMEOUT_S: float = 180.0           # 等待电流达标最长时间

# —— CAN 接口 ——
CAN_INTERFACE: str = "vector"             # vector / pcan / kvaser / socketcan ...
CAN_CHANNEL: int = 0
CAN_BITRATE: int = 500000
CAN_APP_NAME: str = "CANoe"

# —— 唤醒报文 0x795 ——
WAKE_ID: int = 0x795
WAKE_PAYLOAD_HEX: str = "0000000000000000"
WAKE_PERIOD_MS: int = 100

# —— 用于判定“休眠”的周期外发 ID ——
OUT_TX_IDS: List[int | str] = ["0x5A0", "0x603", "0x6C0", "0x6C1"]

# —— 休眠判据 ——
SLEEP_SILENCE_S: float = 3.0       # 连续静默窗口（建议≥外发最大周期×3）
SLEEP_MAX_WAIT_S: float = 180.0    # 最长等待休眠时间
SLEEP_PREDRAIN_S: float = 0.2      # 等待前预排空接收队列

# —— OUT_TX 日志节流（可选） ——
OUT_TX_LOG_THROTTLE_S: float = 0.5  # 每个ID至少间隔这么久才写一条 OUT_TX 日志；为 0 关闭节流

# —— 休眠后唤醒等待（固定 or 随机） ——
WAKE_RANDOM_ENABLED: bool = False
WAKE_FIXED_WAIT_S: float = 60.0
WAKE_MIN_WAIT_S: float = 60.0
WAKE_MAX_WAIT_S: float = 300.0

# —— Ping 目标清单（不要求顺序执行；并行发起，输出按清单顺序展示） ——
ORDER_HOST: List[str] = ["192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202"]
ORDER_MODULE: List[str] = ORDER_HOST + ["14.103.165.189"]

HOST_PROBE_IPS: List[str] = ORDER_HOST[:]         # Host 外部5个
MODULE_PROBE_IPS: List[str] = ORDER_MODULE[:]     # 模组内5+1（含外网 IP）

# —— SSH 连接参数（主/副） ——
SSH_MAIN: Optional[str] = "192.168.1.201"
SSH_BACKUP: Optional[str] = "192.168.1.202"
SSH_USER: str = os.getenv("SSH_USER", "root")
SSH_PASS: Optional[str] = os.getenv("SSH_PASSWORD", "oelinux123") if os.getenv("SSH_PKEY") is None else None
SSH_KEY:  Optional[str] = os.getenv("SSH_PKEY", None)
SSH_PORT: int = int(os.getenv("SSH_PORT", "22"))

# —— Ping 节奏（fast=首次成功就返回；full=满窗统计） ——
PING_MODE: str = os.getenv("PING_MODE", "fast")
PING_TIMEOUT_HOST_S: float = 120.0
PING_TIMEOUT_MODULE_S: float = 120.0
PING_INTERVAL_MS: int = 800
PING_SSH_SINGLE_TIMEOUT_S: int = 2  # 模组内单次 ping 超时（秒）
# Host 侧 Windows ping 的单次 timeout 放到函数内部通过 -w (ms) 指定

# —— SSH 连接“预算窗口”（从电流达标后起算） ——
SSH_CONNECT_TOTAL_BUDGET_S: float = 120.0
SSH_CONNECT_BASE_BACKOFF_S: float = 2.0
SSH_CONNECT_MAX_BACKOFF_S: float = 10.0
SSH_CONNECT_JITTER_S: float = 0.8
SSH_CONNECT_FIRST_TIMEOUT_S: float = 8.0
SSH_CONNECT_RETRY_TIMEOUT_S: float = 8.0

# —— 循环次数 ——
CYCLES: int = 100

# —— 日志目录 ——
LOG_DIR: str = "log2"
# ====================================================================


# -------------------- 工具：ID 解析与日志 --------------------
def _to_int_id(x: int | str) -> int:
    if isinstance(x, int):
        return x
    x = x.strip()
    return int(x, 16) if x.lower().startswith("0x") else int(x)


def _log_event(path: str, obj: Dict[str, Any]):
    obj["ts"] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _open_summary(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ts","cycle","mode","used_random","wake_delay_s",
                "host_first_ok_s","main_first_ok_s","backup_first_ok_s",
                "host_all_first_ok","main_all_first_ok","backup_all_first_ok",
                "stop_to_last_seen_s","sleep_status","sleep_total_wait_s",
                "first_out_tx_seen_ts",
                "host_pass_map","main_pass_map","backup_pass_map",
                "notes"
            ])


def _append_summary(path: str, row: List[Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# -------------------- 795 周期发送（线程实现） --------------------
class PeriodicSender:
    def __init__(self, bus: can.BusABC, arb_id: int, data: bytes, period_s: float, event_cb=None):
        self.bus = bus
        self.id = arb_id
        self.data = data
        self.per = max(0.01, period_s)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._evt = event_cb

    def start(self):
        self._th.start()
        return self

    def _run(self):
        msg = can.Message(arbitration_id=self.id, data=self.data, is_extended_id=False)
        while not self._stop.is_set():
            try:
                self.bus.send(msg)
            except Exception as e:
                if self._evt:
                    self._evt({"evt": "WAKE_SEND_ERR", "err": str(e)})
            self._stop.wait(self.per)

    def stop(self):
        self._stop.set()
        try:
            self._th.join(timeout=1.0)
        except Exception:
            pass


# -------------------- CAN 监听与休眠判定 --------------------
def _predrain(bus: can.BusABC, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        try:
            while bus.recv(timeout=0.0) is not None:
                pass
        except Exception:
            break
        time.sleep(0.01)


def wait_first_seen(bus: can.BusABC, ids: Set[int], timeout_s: float) -> Optional[float]:
    """等待首次见到任一目标ID，返回绝对时间戳；超时返回 None。"""
    _predrain(bus, 0.1)
    end = time.time() + max(0.1, timeout_s)
    while time.time() < end:
        frm = bus.recv(timeout=0.05)
        if frm is None:
            continue
        try:
            if getattr(frm, "is_extended_id", False):
                continue
            arb = int(frm.arbitration_id)
        except Exception:
            continue
        if arb in ids:
            return time.time()
    return None


def wait_until_asleep(bus: can.BusABC,
                      out_tx_ids: Set[int],
                      silence_window_s: float,
                      max_wait_s: float,
                      stop_time: float,
                      event_log: callable,
                      pre_drain_s: float = 0.2,
                      throttle_s: float = OUT_TX_LOG_THROTTLE_S) -> Dict[str, Any]:
    """
    从 stop_time 开始，等待 OUT_TX_IDS 静默 >= silence_window_s。
    返回：
      status: ASLEEP / TIMEOUT
      last_seen: 最后一次见到 OUT_TX 的绝对时间戳
      elapsed_from_stop_s: 停发到最后一次见到 OUT_TX 的间隔
      total_wait_s: 达到静默窗口的休眠达成时间（未达成= None）
      seen_counter: 各 ID 计数
    """
    _predrain(bus, pre_drain_s)

    last_seen: Optional[float] = None
    seen_counter: Dict[int, int] = {}
    start = time.time()
    last_log_ts: Dict[int, float] = {}

    while True:
        now = time.time()
        if now - start > max_wait_s:
            return {
                "status": "TIMEOUT",
                "last_seen": last_seen,
                "silence_window_s": silence_window_s,
                "seen_counter": {hex(k): v for k, v in seen_counter.items()},
                "elapsed_from_stop_s": now - stop_time,
                "total_wait_s": None
            }

        frame = bus.recv(timeout=0.05)
        if frame is None:
            # 判定静默：距离最后一次见到 OUT_TX >= 窗口
            if last_seen is not None and (now - last_seen) >= silence_window_s and now >= stop_time:
                return {
                    "status": "ASLEEP",
                    "last_seen": last_seen,
                    "silence_window_s": silence_window_s,
                    "seen_counter": {hex(k): v for k, v in seen_counter.items()},
                    "elapsed_from_stop_s": last_seen - stop_time if last_seen else None,
                    "total_wait_s": now - stop_time,
                }
            continue

        try:
            if getattr(frame, "is_extended_id", False):
                continue
            arb = int(frame.arbitration_id)
        except Exception:
            continue

        if arb in out_tx_ids:
            ts = time.time()
            last_seen = ts
            seen_counter[arb] = seen_counter.get(arb, 0) + 1
            if throttle_s <= 0:
                event_log({"evt": "OUT_TX", "id": hex(arb), "t": round(ts - stop_time, 3)})
            else:
                prev = last_log_ts.get(arb, 0.0)
                if ts - prev >= throttle_s:
                    event_log({"evt": "OUT_TX", "id": hex(arb), "t": round(ts - stop_time, 3)})
                    last_log_ts[arb] = ts


# -------------------- Windows 专用 Host ping 判定 --------------------
WIN_OK_PATTERNS = [
    re.compile(r"TTL=", re.IGNORECASE),
    re.compile(r"Reply from", re.IGNORECASE),
    re.compile(r"来自.*的回复"),
]

WIN_ERR_SNIPS = [
    "Request timed out", "请求超时",
    "Destination host unreachable", "无法访问目标主机",
    "General failure", "一般故障",
]

def win_ping_once(ip: str, timeout_ms: int = 1000) -> bool:
    """
    使用 Windows 自带 ping：-n 1 只发一个包；-w 毫秒超时。
    成功特征：包含 TTL=/Reply from/来自…的回复，且不含常见错误片段。
    """
    cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=(timeout_ms/1000.0 + 1)).stdout or ""
    except subprocess.TimeoutExpired:
        return False

    if any(p.search(out) for p in WIN_OK_PATTERNS) and not any(s in out for s in WIN_ERR_SNIPS):
        return True
    return False


def _ping_first_ok_host(ip: str,
                        total_timeout_s: float,
                        interval_ms: int,
                        events: callable,
                        mode: str = "fast") -> Tuple[Optional[float], int, int]:
    """
    Host 侧（Windows）对单个 IP 的首次打通时间统计。
    """
    start = time.time()
    attempts = 0
    successes = 0
    events({"evt": "PING_START", "from": "host", "ip": ip,
            "timeout_s": total_timeout_s, "interval_ms": interval_ms, "mode": mode})
    while True:
        ok = win_ping_once(ip, timeout_ms=max(200, interval_ms))  # 单次至少200ms
        attempts += 1
        dt = round(time.time() - start, 3)
        events({"evt": "PING_ATTEMPT", "from": "host", "ip": ip, "ok": ok, "t": dt})
        if ok:
            successes += 1
            if mode == "fast":
                events({"evt": "PING_FIRST_OK", "from": "host", "ip": ip, "first_ok_s": dt})
                return dt, attempts, successes
        if time.time() - start >= total_timeout_s:
            if successes > 0:
                events({"evt": "PING_DONE", "from": "host", "ip": ip,
                        "successes": successes, "first_ok_s": dt})
                return dt, attempts, successes
            else:
                events({"evt": "PING_TIMEOUT", "from": "host", "ip": ip, "waited_s": dt})
                return None, attempts, successes
        time.sleep(max(0.0, interval_ms/1000.0))


# -------------------- SSH 连接重试（指数退避 + 抖动 + 预算窗口） --------------------
def _connect_ssh_with_retry(label: str,
                            host: str,
                            events: callable,
                            user: str,
                            password: Optional[str],
                            pkey_path: Optional[str],
                            port: int,
                            total_budget_s: float,
                            base_backoff_s: float,
                            max_backoff_s: float,
                            jitter_s: float,
                            first_timeout_s: float,
                            retry_timeout_s: float) -> Optional[SSHClient]:
    deadline = time.time() + max(1.0, total_budget_s)
    attempt = 0
    backoff = max(0.1, base_backoff_s)
    while time.time() < deadline:
        attempt += 1
        timeout_s = first_timeout_s if attempt == 1 else retry_timeout_s
        try:
            events({"evt": "SSH_CONNECT_TRY", "from": label, "ssh_host": host,
                    "attempt": attempt, "timeout_s": timeout_s})
            ssh = SSHClient(host, port=port, user=user,
                            password=password, pkey_path=pkey_path, timeout=timeout_s)
            ssh.connect()
            events({"evt": "SSH_CONNECT_OK", "from": label, "ssh_host": host, "attempt": attempt})
            return ssh
        except Exception as e:
            wait_s = min(max_backoff_s, backoff) + random.uniform(0.0, max(0.0, jitter_s))
            remain = deadline - time.time()
            events({"evt": "SSH_CONNECT_RETRY", "from": label, "ssh_host": host,
                    "attempt": attempt, "error": str(e),
                    "sleep_s": round(wait_s, 2), "remain_s": round(max(0.0, remain), 2)})
            if remain <= 0:
                break
            time.sleep(min(wait_s, max(0.0, remain)))
            backoff *= 2.0
    events({"evt": "SSH_CONNECT_GIVEUP", "from": label, "ssh_host": host})
    return None


# -------------------- 模组内并行 ping（基于 SSH） --------------------
def _pings_ssh_parallel(label: str,
                        host: Optional[str],
                        ips: List[str],
                        total_timeout_s: float,
                        interval_ms: int,
                        single_timeout_s: int,
                        events: callable) -> Dict[str, Any]:
    """
    并行对 ips 逐个做 ping（fast/full 由全局 PING_MODE 控制）。
    如果未连上 SSH，则返回 {"__error__": "..."} 上层统一 FAIL。
    """
    if not host:
        return {}

    # SSH 预算：假定外层已 wait_current_threshold 达标后才进来，这里直接用 120s 预算
    ssh = _connect_ssh_with_retry(
        label=label, host=host, events=events,
        user=SSH_USER, password=SSH_PASS, pkey_path=SSH_KEY, port=SSH_PORT,
        total_budget_s=SSH_CONNECT_TOTAL_BUDGET_S,
        base_backoff_s=SSH_CONNECT_BASE_BACKOFF_S,
        max_backoff_s=SSH_CONNECT_MAX_BACKOFF_S,
        jitter_s=SSH_CONNECT_JITTER_S,
        first_timeout_s=SSH_CONNECT_FIRST_TIMEOUT_S,
        retry_timeout_s=SSH_CONNECT_RETRY_TIMEOUT_S,
    )
    if ssh is None:
        err = f"SSH connect failed within {int(SSH_CONNECT_TOTAL_BUDGET_S)}s after power>={int(POWER_WAKE_THRESHOLD_MA)}mA"
        events({"evt": "SSH_ERROR", "from": label, "ssh_host": host, "error": err})
        return {"__error__": err}

    res: Dict[str, Any] = {}
    try:
        def one_ip(ip: str) -> Tuple[str, Dict[str, Any]]:
            start = time.time()
            attempts = 0
            successes = 0
            events({"evt": "PING_START", "from": label, "ssh_host": host, "ip": ip,
                    "timeout_s": total_timeout_s, "interval_ms": interval_ms, "mode": PING_MODE})
            while True:
                ok = ssh.ping(ip, count=1, timeout=max(1, int(single_timeout_s)))
                attempts += 1
                dt = round(time.time() - start, 3)
                events({"evt": "PING_ATTEMPT", "from": label, "ssh_host": host, "ip": ip, "ok": ok, "t": dt})
                if ok:
                    successes += 1
                    if PING_MODE == "fast":
                        events({"evt": "PING_FIRST_OK", "from": label, "ssh_host": host, "ip": ip, "first_ok_s": dt})
                        return ip, {"first_ok": dt, "attempts": attempts, "successes": successes}
                if time.time() - start >= total_timeout_s:
                    if successes > 0:
                        events({"evt": "PING_DONE", "from": label, "ssh_host": host, "ip": ip,
                                "successes": successes, "first_ok_s": dt})
                        return ip, {"first_ok": dt, "attempts": attempts, "successes": successes}
                    else:
                        events({"evt": "PING_TIMEOUT", "from": label, "ssh_host": host, "ip": ip, "waited_s": dt})
                        return ip, {"first_ok": None, "attempts": attempts, "successes": successes}
                time.sleep(max(0.0, interval_ms/1000.0))

        with ThreadPoolExecutor(max_workers=min(8, len(ips))) as ex:
            futs = {ex.submit(one_ip, ip): ip for ip in ips}
            for fut in as_completed(futs):
                ip, data = fut.result()
                res[ip] = data
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return res


# -------------------- 汇总辅助 --------------------
def summarize_first_ok_map(res: Dict[str, Any]) -> Dict[str, Optional[float]]:
    m: Dict[str, Optional[float]] = {}
    for k, v in res.items():
        if isinstance(v, dict) and "first_ok" in v:
            m[k] = v["first_ok"]
    return m


def tmax_first_ok(res_map: Dict[str, Optional[float]]) -> Optional[float]:
    vals = [float(t) for t in res_map.values() if t is not None]
    return max(vals) if vals else None


def all_first_ok(res_map: Dict[str, Optional[float]]) -> Optional[bool]:
    if not res_map:
        return None
    return all((t is not None) for t in res_map.values())


def to_pass_map(res: Dict[str, Any], order: List[str], ssh_error: Optional[str] = None) -> Dict[str, bool]:
    """
    将一次 ping 结果转成 {ip: True/False}，按给定顺序补足缺失项。
    若 ssh_error 不为空，统一 False。
    """
    out: Dict[str, bool] = {}
    if ssh_error:
        for ip in order:
            out[ip] = False
        return out
    for ip in order:
        v = res.get(ip)
        if isinstance(v, dict) and ("first_ok" in v):
            out[ip] = (v["first_ok"] is not None)
        else:
            out[ip] = False
    return out


def format_report_block(title: str, order: List[str], res: Dict[str, Any], ssh_error: Optional[str] = None) -> Tuple[str, Dict[str, bool]]:
    lines = [f"=== {title} ==="]
    pass_map = to_pass_map(res, order, ssh_error)
    for ip in order:
        if ssh_error:
            lines.append(f"{ip:<15} FAIL ❌  原因: {ssh_error}")
            continue
        v = res.get(ip, {})
        first_ok = v.get("first_ok") if isinstance(v, dict) else None
        attempts = v.get("attempts") if isinstance(v, dict) else 0
        status = "PASS ✅" if (first_ok is not None) else "FAIL ❌"
        extra = f"首通 {first_ok:.3f}s" if first_ok is not None else "未打通"
        lines.append(f"{ip:<15} {status}  尝试{attempts:<3} {extra}")
    return "\n".join(lines), pass_map


# -------------------- 电流电源门槛 --------------------
def wait_current_threshold(psu: Optional[OwonPSU],
                           threshold_ma: float,
                           hold_ms: int,
                           timeout_s: float,
                           event: callable) -> bool:
    """
    返回 True 表示在超时内达标；False 表示超时未达标（会继续流程，但日志有 WARN）。
    """
    if psu is None:
        event({"evt": "POWER_HINT", "msg": "无PSU，跳过电流门槛判据"})
        return True

    end = time.time() + max(1.0, timeout_s)
    ok_since: Optional[float] = None
    while time.time() < end:
        try:
            ia = psu.measure_current()  # A
        except Exception as e:
            event({"evt": "POWER_ERR", "msg": f"读电流异常：{e}"})
            time.sleep(0.2)
            continue
        ma = ia * 1000.0
        event({"evt": "POWER_SAMPLE", "ma": round(ma, 1)})
        if ma >= threshold_ma:
            ok_since = ok_since or time.time()
            if (time.time() - ok_since) * 1000.0 >= hold_ms:
                event({"evt": "POWER_OK", "ma": round(ma, 1)})
                return True
        else:
            ok_since = None
        time.sleep(0.2)

    event({"evt": "POWER_TIMEOUT", "threshold_ma": threshold_ma})
    return False


# -------------------- 主流程 --------------------
class Runner:
    def __init__(self):
        # 解析 OUT_TX_IDS
        self.out_tx_ids: Set[int] = {_to_int_id(x) for x in OUT_TX_IDS}

        # 日志路径
        os.makedirs(LOG_DIR, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        self.events_path = os.path.join(LOG_DIR, f"sleep_wake_events_{day}.log")
        self.summary_path = os.path.join(LOG_DIR, f"sleep_wake_summary_{day}.csv")
        _open_summary(self.summary_path)

        # 打开 CAN
        self.canx = CanBus(interface=CAN_INTERFACE, channel=CAN_CHANNEL, bitrate=CAN_BITRATE, app_name=CAN_APP_NAME)
        self.bus: can.BusABC = self.canx.bus

        # 电源（可选）
        self.psu: Optional[OwonPSU] = None
        if USE_PSU:
            try:
                psu = OwonPSU(port=PSU_COM, baud=PSU_BAUD, timeout=1.0)
                if PSU_SET_OUTPUT:
                    psu.set_voltage(PSU_VOLT_SET)
                    psu.set_current(PSU_CURR_LIMIT_A)
                    # ----------- 方案一：强制冷启动 -----------
                    psu.off()
                    time.sleep(2)  # 等待2秒，确保完全掉电
                    psu.on()
                    # ----------------------------------------
                    self._event({
                        "evt": "PSU_ON",
                        "volt": PSU_VOLT_SET,
                        "curr_limit_A": PSU_CURR_LIMIT_A,
                        "note": "cold boot enforced"
                    })
                else:
                    self._event({"evt": "PSU_READONLY", "msg": "脚本不控制上电，仅读取电流"})
                self.psu = psu
            except Exception as e:
                self._event({"evt": "PSU_WARN", "msg": f"电源不可用：{e}"})
                self.psu = None

    def _event(self, obj: Dict[str, Any]):
        _log_event(self.events_path, obj)

    # Host 侧：并行 Ping
    def _pings_host_parallel(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(HOST_PROBE_IPS))) as ex:
            futs = {
                ex.submit(_ping_first_ok_host, ip, PING_TIMEOUT_HOST_S, PING_INTERVAL_MS, self._event, PING_MODE): ip
                for ip in HOST_PROBE_IPS
            }
            for fut in as_completed(futs):
                ip = futs[fut]
                t, attempts, successes = fut.result()
                res[ip] = {"first_ok": t, "attempts": attempts, "successes": successes}
        return res

    # 模组内：并行 Ping（带 SSH 预算 120s）
    def _pings_ssh_parallel(self, label: str, host: Optional[str]) -> Dict[str, Any]:
        if not host:
            return {}
        return _pings_ssh_parallel(
            label=label, host=host, ips=MODULE_PROBE_IPS,
            total_timeout_s=PING_TIMEOUT_MODULE_S,
            interval_ms=PING_INTERVAL_MS,
            single_timeout_s=PING_SSH_SINGLE_TIMEOUT_S,
            events=self._event,
        )

    def _pick_wake_delay(self) -> Tuple[bool, float]:
        if WAKE_RANDOM_ENABLED:
            lo, hi = min(WAKE_MIN_WAIT_S, WAKE_MAX_WAIT_S), max(WAKE_MIN_WAIT_S, WAKE_MAX_WAIT_S)
            return True, random.uniform(lo, hi)
        return False, max(0.0, WAKE_FIXED_WAIT_S)

    def _wake_start(self) -> PeriodicSender:
        payload = bytes.fromhex(WAKE_PAYLOAD_HEX)
        per = max(0.02, WAKE_PERIOD_MS/1000.0)
        task = PeriodicSender(self.bus, WAKE_ID, payload, per, event_cb=self._event).start()
        self._event({"evt": "WAKE_START", "id": hex(WAKE_ID), "period_ms": WAKE_PERIOD_MS, "payload": WAKE_PAYLOAD_HEX})
        return task

    def _wake_stop(self, task: Optional[PeriodicSender]):
        if task:
            try:
                task.stop()
            except Exception:
                pass
        self._event({"evt": "WAKE_STOP"})

    # 控制台报告（你最关心）
    def _print_cycle_report(self, cycle: int,
                            host_res: Dict[str, Any],
                            main_res: Dict[str, Any],
                            backup_res: Dict[str, Any],
                            sd_res: Dict[str, Any]) -> Tuple[Dict[str, bool], Dict[str, bool], Dict[str, bool]]:
        print("\n===== 测试结果 =====")
        print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  轮次 #{cycle}")

        # 主/副模组 SSH 是否失败
        main_err = main_res.get("__error__")
        backup_err = backup_res.get("__error__")

        block_host, host_pass_map = format_report_block("外部 Ping", ORDER_HOST, host_res)
        print(block_host)

        block_main, main_pass_map = format_report_block("主模组内 Ping", ORDER_MODULE, main_res, main_err)
        print(block_main)

        block_backup, backup_pass_map = format_report_block("副模组内 Ping", ORDER_MODULE, backup_res, backup_err)
        print(block_backup)

        # 休眠时长（双指标）
        sleep_status = sd_res.get("status")
        stop_to_last = sd_res.get("elapsed_from_stop_s")
        sleep_reached = sd_res.get("total_wait_s")  # 满足静默窗所用时长
        print("\n=== 休眠判定 ===")
        print(f"状态：{sleep_status}  |  判据窗口：{SLEEP_SILENCE_S}s")
        print(f"stop→last_tx_seen_s：{None if stop_to_last is None else round(stop_to_last,3)}")
        print(f"sleep_reached_s ：{None if sleep_reached is None else round(sleep_reached,3)}")

        return host_pass_map, main_pass_map, backup_pass_map

    def run(self):
        try:
            # ===== 首次上电 =====
            mode = "wake_on_boot" if FIRST_BOOT_SEND_WAKE else "free_run"
            boot_task: Optional[PeriodicSender] = None
            first_out_tx_seen_ts: Optional[float] = None

            if FIRST_BOOT_SEND_WAKE:
                boot_task = self._wake_start()
            else:
                first_out_tx_seen_ts = wait_first_seen(self.bus, self.out_tx_ids, timeout_s=60.0)
                if first_out_tx_seen_ts:
                    self._event({"evt": "FIRST_OUT_TX_SEEN", "t": first_out_tx_seen_ts})

            # —— 等电流达标（>400mA 且保持），之后才开始 SSH 预算计时思想 ——
            wait_current_threshold(
                psu=self.psu,
                threshold_ma=POWER_WAKE_THRESHOLD_MA,
                hold_ms=POWER_HOLD_MS,
                timeout_s=POWER_TIMEOUT_S,
                event=self._event
            )

            # 三路并行 ping
            host_res = self._pings_host_parallel()
            main_res = self._pings_ssh_parallel("main", SSH_MAIN)
            backup_res = self._pings_ssh_parallel("backup", SSH_BACKUP)

            host_map = summarize_first_ok_map(host_res)
            main_map = summarize_first_ok_map(main_res) if "__error__" not in main_res else {}
            backup_map = summarize_first_ok_map(backup_res) if "__error__" not in backup_res else {}

            host_first_ok = tmax_first_ok(host_map)
            main_first_ok = tmax_first_ok(main_map) if main_map else None
            backup_first_ok = tmax_first_ok(backup_map) if backup_map else None

            # 停止 0x795/确定计时基准
            if FIRST_BOOT_SEND_WAKE:
                self._wake_stop(boot_task)
                stop_time = time.time()
            else:
                stop_time = first_out_tx_seen_ts or time.time()

            # 等待休眠
            sd_res = wait_until_asleep(
                bus=self.bus,
                out_tx_ids=self.out_tx_ids,
                silence_window_s=SLEEP_SILENCE_S,
                max_wait_s=SLEEP_MAX_WAIT_S,
                stop_time=stop_time,
                event_log=self._event,
                pre_drain_s=SLEEP_PREDRAIN_S
            )

            # 控制台报告
            host_pass_map, main_pass_map, backup_pass_map = self._print_cycle_report(
                cycle=1, host_res=host_res, main_res=main_res, backup_res=backup_res, sd_res=sd_res
            )

            # 写 CSV
            notes_obj = {
                "host_first_ok_map": host_map,
                "main_first_ok_map": main_map if main_map else None,
                "backup_first_ok_map": backup_map if backup_map else None,
                "seen_counter": sd_res.get("seen_counter", {}),
                "main_error": main_res.get("__error__"),
                "backup_error": backup_res.get("__error__"),
            }
            _append_summary(self.summary_path, [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1, mode, None, None,
                None if host_first_ok is None else round(host_first_ok, 3),
                None if main_first_ok is None else round(main_first_ok, 3),
                None if backup_first_ok is None else round(backup_first_ok, 3),
                all_first_ok(host_map),
                all_first_ok(main_map) if main_map else False if main_res.get("__error__") else None,
                all_first_ok(backup_map) if backup_map else False if backup_res.get("__error__") else None,
                None if sd_res.get("elapsed_from_stop_s") is None else round(sd_res.get("elapsed_from_stop_s"), 3),
                sd_res.get("status"),
                None if sd_res.get("total_wait_s") is None else round(sd_res.get("total_wait_s"), 3),
                None if first_out_tx_seen_ts is None else round(first_out_tx_seen_ts, 3),
                json.dumps(host_pass_map, ensure_ascii=False),
                json.dumps(main_pass_map, ensure_ascii=False),
                json.dumps(backup_pass_map, ensure_ascii=False),
                json.dumps(notes_obj, ensure_ascii=False),
            ])

            print(
                f"[#1] host_first_ok_tmax={None if host_first_ok is None else round(host_first_ok,3)}s | "
                f"sleep_status={sd_res.get('status')} | "
                f"stop→last_tx_seen={sd_res.get('elapsed_from_stop_s')}s | "
                f"sleep_reached={sd_res.get('total_wait_s')}s"
            )

            # ===== 后续循环 =====
            for cycle in range(2, CYCLES + 1):
                used_random, delay_s = self._pick_wake_delay()
                time.sleep(delay_s)

                # 唤醒：开始发 0x795
                task = self._wake_start()

                # 等电流达标（>400mA）
                wait_current_threshold(
                    psu=self.psu,
                    threshold_ma=POWER_WAKE_THRESHOLD_MA,
                    hold_ms=POWER_HOLD_MS,
                    timeout_s=POWER_TIMEOUT_S,
                    event=self._event
                )

                # 三路并行 ping
                host_res = self._pings_host_parallel()
                main_res = self._pings_ssh_parallel("main", SSH_MAIN)
                backup_res = self._pings_ssh_parallel("backup", SSH_BACKUP)

                host_map = summarize_first_ok_map(host_res)
                main_map = summarize_first_ok_map(main_res) if "__error__" not in main_res else {}
                backup_map = summarize_first_ok_map(backup_res) if "__error__" not in backup_res else {}

                host_first_ok = tmax_first_ok(host_map)
                main_first_ok = tmax_first_ok(main_map) if main_map else None
                backup_first_ok = tmax_first_ok(backup_map) if backup_map else None

                # 停发 0x795，计时
                self._wake_stop(task)
                stop_time = time.time()

                # 等待休眠
                sd_res = wait_until_asleep(
                    bus=self.bus,
                    out_tx_ids=self.out_tx_ids,
                    silence_window_s=SLEEP_SILENCE_S,
                    max_wait_s=SLEEP_MAX_WAIT_S,
                    stop_time=stop_time,
                    event_log=self._event,
                    pre_drain_s=SLEEP_PREDRAIN_S
                )

                # 控制台报告
                host_pass_map, main_pass_map, backup_pass_map = self._print_cycle_report(
                    cycle=cycle, host_res=host_res, main_res=main_res, backup_res=backup_res, sd_res=sd_res
                )

                # 写 CSV
                notes_obj = {
                    "host_first_ok_map": host_map,
                    "main_first_ok_map": main_map if main_map else None,
                    "backup_first_ok_map": backup_map if backup_map else None,
                    "seen_counter": sd_res.get("seen_counter", {}),
                    "main_error": main_res.get("__error__"),
                    "backup_error": backup_res.get("__error__"),
                }
                _append_summary(self.summary_path, [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cycle,
                    "loop", used_random, round(delay_s, 1),
                    None if host_first_ok is None else round(host_first_ok, 3),
                    None if main_first_ok is None else round(main_first_ok, 3),
                    None if backup_first_ok is None else round(backup_first_ok, 3),
                    all_first_ok(host_map),
                    all_first_ok(main_map) if main_map else False if main_res.get("__error__") else None,
                    all_first_ok(backup_map) if backup_map else False if backup_res.get("__error__") else None,
                    None if sd_res.get("elapsed_from_stop_s") is None else round(sd_res.get("elapsed_from_stop_s"), 3),
                    sd_res.get("status"),
                    None if sd_res.get("total_wait_s") is None else round(sd_res.get("total_wait_s"), 3),
                    None,
                    json.dumps(host_pass_map, ensure_ascii=False),
                    json.dumps(main_pass_map, ensure_ascii=False),
                    json.dumps(backup_pass_map, ensure_ascii=False),
                    json.dumps(notes_obj, ensure_ascii=False),
                ])

                print(
                    f"[#{cycle}] wait={round(delay_s,1)}s({'rand' if used_random else 'fixed'}) | "
                    f"host_first_ok_tmax={None if host_first_ok is None else round(host_first_ok,3)}s | "
                    f"sleep_status={sd_res.get('status')} | "
                    f"stop→last_tx_seen={sd_res.get('elapsed_from_stop_s')}s | "
                    f"sleep_reached={sd_res.get('total_wait_s')}s"
                )

        finally:
            # 资源释放
            try:
                self.canx.close()
            except Exception:
                pass
            if self.psu:
                try:
                    if PSU_SET_OUTPUT and not PSU_LEAVE_ON_AT_EXIT:
                        self.psu.off()
                        self._event({"evt": "PSU_OFF"})
                    elif PSU_SET_OUTPUT and PSU_LEAVE_ON_AT_EXIT:
                        self._event({"evt": "PSU_LEAVE_ON", "volt": PSU_VOLT_SET})
                except Exception as e:
                    self._event({"evt": "PSU_CLOSE_WARN", "msg": str(e)})


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    Runner().run()


if __name__ == "__main__":
    main()
