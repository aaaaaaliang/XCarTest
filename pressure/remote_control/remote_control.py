#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBOX 上/下电（6D0）→ 唤醒验证（>400mA 发 0x795 + 并发 Ping）→ 停 795 等休眠的全流程监测脚本
—— 冻结规范 v1.0 实现版
"""

from __future__ import annotations
import os
import sys
import time
import json
import csv
import threading
import queue
import random
import subprocess
import platform
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any


# 可选 YAML 配置支持
try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# CAN & SSH 依赖（未安装时给出提示）
try:
    import can  # python-can
except Exception:
    can = None

try:
    import paramiko
except Exception:
    paramiko = None


# =========================
# 1) 全局配置（可编辑）
# =========================

CONFIG = {
    # ---- 基本运行参数 ----
    "CSV_PATH": "sleepwake_monitor.csv",
    "JSONL_PATH": "sleepwake_monitor.jsonl",
    "LOG_PRINT": True,                      # 控制台打印
    "TZ_OFFSET_MIN": 0,                     # 日志时区偏移（分钟），0 表示系统本地时间

    # ---- 窗口/阈值 & 判据 ----
    "WINDOW_SLA_MIN": 60,                   # 60 分钟窗口 PASS 判据
    "WINDOW_HARD_MIN": 70,                  # 70 分钟强制上限（未见下一次上电01则记 FAIL 并重置）
    "GT300_WITHIN_3M": True,                # 是否记录上电后3分钟内>300mA
    "GT300_THRESHOLD_MA": 300,
    "WAKE_THRESHOLD_MA": 400,               # >400mA 视为充分唤醒，开始发795+Ping
    "SLEEP_THRESHOLD_MA": 120,              # 休眠电流阈值（可按你的实际调整）
    "SLEEP_SILENCE_SEC": 3.0,               # 判定“四报文停止”的静默窗口（秒）
    "SLEEP_REACH_TIMEOUT_SEC": 5 * 60,      # 停795后，5分钟内需达到休眠

    # ---- 6D0/795 & 四报文 ----
    "ID_6D0": 0x6D0,
    "ID_795": 0x795,
    "FOUR_IDS": [0x5A0, 0x603, 0x6C0, 0x6C1],
    "ID_6D0_UP_DATA": [0x01, 0, 0, 0, 0, 0, 0, 0],   # 上电 01
    "ID_6D0_DOWN_DATA": [0x04, 0, 0, 0, 0, 0, 0, 0], # 下电 04
    "FRAME_PAYLOAD_LEN": 8,

    # ---- 795 持续发送 ----
    "SEND_795_PERIOD_MS": 100,              # 795 发送周期（毫秒）
    "SEND_795_DATA": [0x00]*8,              # 795 数据（按需要修改）
    "SEND_795_EXTENDED": False,             # 标准帧 False

    # ---- CAN 总线参数（按你的设备修改）----
    # 例如：Vector: bustype='vector', app_name='CANalyzer', channel=0, bitrate=500000
    #       PCAN:   bustype='pcan', channel='PCAN_USBBUS1', bitrate=500000
    #       SocketCAN: bustype='socketcan', channel='can0', bitrate不需要
    "CAN": {
        "bustype": "vector",                 # 'vector' / 'pcan' / 'socketcan' / ...
        "channel": 0,
        "bitrate": 500000,
        "app_name": None,                   # Vector 可指定 'CANalyzer'/'CANoe'，不需要则为 None
    },

    # ---- 电流读取源（任选其一）----
    # mode = 'file'：从 JSONL 文件尾部读取 {"evt":"POWER_SAMPLE","ma":xxx}
    # mode = 'scpi'：通过 SCPI 读取电源电流（示例实现为占位，你可替换为你的设备命令）
    # mode = 'dummy'：调试用，产生随机电流
    "POWER_SOURCE": {
        "mode": "serial",                      # 'file' | 'scpi' | 'dummy'
        "file_path": "power_samples.jsonl",  # mode=file 时：采样文件路径
        # mode=scpi 示例参数
        "scpi": {
            "host": "192.168.1.50",
            "port": 5025,
            "cmd": "MEAS:CURR?",            # 设备查询电流的命令（单位 A），示例
            "timeout_sec": 2.0
        },
        "dummy": {
            "lo_ma": 80,
            "hi_ma": 450
        },
        "poll_interval_sec": 0.5
    },

    # ---- Ping 目标清单（按你给的三类）----
    # Host（外部，主机本机发起）
    "PING_HOST_TARGETS": [
        "192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202"
    ],
    # Main/Backup（SSH 登入模组内执行），并包含公网地址
    "PING_MAIN_TARGETS": [
        "192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202", "14.103.165.189"
    ],
    "PING_BACKUP_TARGETS": [
        "192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202", "14.103.165.189"
    ],

    # ---- SSH 参数 & 重试策略 ----
    "SSH_MAIN": {"host": "192.168.1.201", "port": 22, "user": "root", "password": "root"},
    "SSH_BACKUP": {"host": "192.168.1.202", "port": 22, "user": "root", "password": "root"},
    "SSH_CONNECT_BUDGET_SEC": 300,          # SSH 连接总预算（建议充足）
    "SSH_BACKOFF_BASE_SEC": 1.2,            # 初始退避
    "SSH_BACKOFF_FACTOR": 1.8,              # 乘数
    "SSH_BACKOFF_JITTER_SEC": 0.8,          # 抖动上限（0~此值之间随机）

    # ---- Ping 重试策略 ----
    "PING_PER_IP_TIMEOUT_SEC": 2.0,         # 单次 ping 等待
    "PING_PER_IP_BUDGET_SEC": 60.0,         # 每个 IP 总预算（>400mA 后开始计）
    "PING_PARALLELISM": 8,                  # 并发度

    # ---- 其他 ----
    "NOTES": "按冻结规范 v1.0；所有参数可在此处调整或通过 --config 外部配置覆盖。",
}


# =========================
# 2) 工具函数
# =========================

def now_dt() -> datetime:
    return datetime.now()

def fmt_ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None: return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def monotonic() -> float:
    return time.monotonic()

def sleep_s(sec: float):
    time.sleep(max(0.0, sec))

def log_print(enabled: bool, msg: str):
    if enabled:
        print(msg, flush=True)

def load_config_from_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        if yaml is None:
            raise RuntimeError("需要 pyyaml 以加载 YAML 配置（pip install pyyaml）")
        return yaml.safe_load(text)
    else:
        return json.loads(text)

def merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge_dict(dst[k], v)
        else:
            dst[k] = v
    return dst

def safe_int(b: int) -> int:
    return int(b) & 0xFF


# =========================
# 3) 电流读取源
# =========================

class PowerSource:
    def start(self): ...
    def stop(self): ...
    def last_ma(self) -> Optional[float]: ...
    def snapshot(self) -> Dict[str, Any]: ...

class SerialPowerSource(PowerSource):
    """通过串口 (pyserial) + SCPI 协议读取电流，单位 mA"""
    def __init__(self, port: str = "COM3", baud: int = 115200, timeout: float = 1.0,
                 poll_interval_sec: float = 0.5, logger=lambda x: None):
        import serial
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.poll = poll_interval_sec
        self._stop = threading.Event()
        self._ma: Optional[float] = None
        self._th = None
        self._logger = logger

        # 初始化串口
        try:
            self.ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError(f"[SerialPowerSource] 打开串口失败: {e}")

    def _query(self, cmd: str) -> str:
        """发送命令并返回响应"""
        try:
            self.ser.write((cmd + "\r\n").encode())
            time.sleep(0.1)
            return self.ser.read_all().decode(errors="ignore").strip()
        except Exception as e:
            self._logger(f"[SerialPowerSource] 命令 {cmd} 失败: {e}")
            return ""

    def _run(self):
        while not self._stop.is_set():
            try:
                resp = self._query("MEAS:CURR?")
                if resp:
                    amps = float(resp)  # 电源返回单位 A
                    self._ma = amps * 1000.0  # 转换成 mA
            except Exception as e:
                self._logger(f"[SerialPowerSource] 采样异常: {e}")
            sleep_s(self.poll)

    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="PowerSerial", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)
        try:
            self.ser.close()
        except Exception:
            pass

    def last_ma(self) -> Optional[float]:
        return self._ma

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mode": "serial",
            "port": self.port,
            "baud": self.baud,
            "last_ma": self._ma
        }


class FileTailPowerSource(PowerSource):
    """从 JSONL 文件尾部读取 {"evt":"POWER_SAMPLE","ma":310.0,"ts":"HH:MM:SS.xxx"}"""
    def __init__(self, file_path: str, poll_interval_sec: float, logger=lambda x: None):
        self.file_path = file_path
        self.poll = poll_interval_sec
        self._stop = threading.Event()
        self._ma: Optional[float] = None
        self._th = None
        self._logger = logger

    def _run(self):
        # 简易 tail -f
        try:
            f = open(self.file_path, "r", encoding="utf-8")
            f.seek(0, os.SEEK_END)
        except FileNotFoundError:
            self._logger(f"[POWER] 文件不存在：{self.file_path}，等待创建...")
            while not self._stop.is_set():
                if os.path.exists(self.file_path):
                    try:
                        f = open(self.file_path, "r", encoding="utf-8")
                        f.seek(0, os.SEEK_END)
                        break
                    except Exception:
                        pass
                sleep_s(self.poll)
        except Exception as e:
            self._logger(f"[POWER] 打开文件失败：{e}")
            return

        while not self._stop.is_set():
            line = f.readline()
            if not line:
                sleep_s(self.poll)
                continue
            try:
                obj = json.loads(line.strip())
                if obj.get("evt") == "POWER_SAMPLE":
                    ma = float(obj.get("ma"))
                    self._ma = ma
            except Exception:
                continue

        try:
            f.close()
        except Exception:
            pass

    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="PowerTail", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)

    def last_ma(self) -> Optional[float]:
        return self._ma

    def snapshot(self) -> Dict[str, Any]:
        return {"mode": "file", "file_path": self.file_path, "last_ma": self._ma}


class ScpiPowerSource(PowerSource):
    """占位示例：通过 TCP-SCPI 读取电流（单位 A），转换为 mA。请按你的电源设备命令修正。"""
    def __init__(self, host: str, port: int, cmd: str, timeout_sec: float, poll_interval_sec: float, logger=lambda x: None):
        self.host = host
        self.port = port
        self.cmd = cmd
        self.timeout = timeout_sec
        self.poll = poll_interval_sec
        self._stop = threading.Event()
        self._ma: Optional[float] = None
        self._th = None
        self._logger = logger

    def _run(self):
        import socket
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                    s.settimeout(self.timeout)
                    while not self._stop.is_set():
                        s.sendall((self.cmd + "\n").encode("ascii"))
                        buf = s.recv(128).decode("ascii", errors="ignore").strip()
                        # 假设返回安培值，如 "0.315"
                        try:
                            amps = float(buf)
                            self._ma = amps * 1000.0
                        except Exception:
                            pass
                        sleep_s(self.poll)
            except Exception as e:
                self._logger(f"[POWER] SCPI 连接失败：{e}，重试中...")
                sleep_s(self.poll)

    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="PowerSCPI", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)

    def last_ma(self) -> Optional[float]:
        return self._ma

    def snapshot(self) -> Dict[str, Any]:
        return {"mode": "scpi", "host": self.host, "port": self.port, "last_ma": self._ma}


class DummyPowerSource(PowerSource):
    def __init__(self, lo_ma=80, hi_ma=450, poll_interval_sec=0.5):
        self.lo = lo_ma
        self.hi = hi_ma
        self.poll = poll_interval_sec
        self._stop = threading.Event()
        self._ma: Optional[float] = None
        self._th = None

    def _run(self):
        while not self._stop.is_set():
            self._ma = random.uniform(self.lo, self.hi)
            sleep_s(self.poll)

    def start(self):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="PowerDummy", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=2.0)

    def last_ma(self) -> Optional[float]:
        return self._ma

    def snapshot(self) -> Dict[str, Any]:
        return {"mode": "dummy", "last_ma": self._ma, "range": [self.lo, self.hi]}


# =========================
# 4) CAN 监听/发送
# =========================

class CANMonitor:
    def __init__(self, cfg: Dict[str, Any], logger=lambda x: None):
        if can is None:
            raise RuntimeError("未安装 python-can，请先 pip install python-can")
        self.cfg = cfg
        self.logger = logger
        self.bus: Optional[can.Bus] = None
        self._stop = threading.Event()
        self._th = None
        self.msg_q: "queue.Queue[can.Message]" = queue.Queue(maxsize=1000)
        self.last_seen: Dict[int, float] = {}  # four-ids 最后一次看到的单调时刻
        self._notifier = None

    def start(self):
        bus_cfg = self.cfg["CAN"]
        bustype = bus_cfg.get("bustype")
        channel = bus_cfg.get("channel")
        bitrate = bus_cfg.get("bitrate")
        app_name = bus_cfg.get("app_name")

        kwargs = {"bustype": bustype, "channel": channel}
        if bitrate and bustype not in ("socketcan",):
            kwargs["bitrate"] = bitrate
        if bustype == "vector" and app_name:
            kwargs["app_name"] = app_name

        self.bus = can.interface.Bus(**kwargs)
        self._stop.clear()
        self._th = threading.Thread(target=self._recv_loop, name="CANRecv", daemon=True)
        self._th.start()
        self.logger("[CAN] 接收线程启动")

    def _recv_loop(self):
        assert self.bus is not None
        while not self._stop.is_set():
            try:
                msg = self.bus.recv(timeout=0.2)
                if msg is None:
                    continue
                # 记录四报文的最后出现时刻（单调时钟）
                if msg.arbitration_id in CONFIG["FOUR_IDS"]:
                    self.last_seen[msg.arbitration_id] = monotonic()
                # 投递给上层
                try:
                    self.msg_q.put_nowait(msg)
                except queue.Full:
                    pass
            except Exception as e:
                self.logger(f"[CAN] 接收异常：{e}")
                sleep_s(0.2)

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=1.0)
        try:
            if self.bus:
                self.bus.shutdown()
        except Exception:
            pass

    def send_795_once(self, data: List[int], is_extended_id: bool = False):
        assert self.bus is not None
        dlc = min(len(data), CONFIG["FRAME_PAYLOAD_LEN"])
        msg = can.Message(arbitration_id=CONFIG["ID_795"],
                          is_extended_id=is_extended_id,
                          data=bytes([safe_int(x) for x in data[:dlc]]))
        self.bus.send(msg)

    def send_795_loop(self, stop_evt: threading.Event, period_ms: int, data: List[int], is_extended_id: bool = False):
        self.logger("[795] 保持唤醒发送线程启动")
        per = max(10, period_ms) / 1000.0
        while not stop_evt.is_set():
            try:
                self.send_795_once(data, is_extended_id)
            except Exception as e:
                self.logger(f"[795] 发送异常：{e}")
            sleep_s(per)
        self.logger("[795] 保持唤醒发送线程退出")

    def wait_four_ids_stopped(self, since_ts: float, silence_sec: float, timeout_sec: float) -> Tuple[bool, float]:
        """
        观察自 since_ts 时刻起，四个报文在某一刻之后都“沉默 >= silence_sec”；
        返回 (是否在 timeout 内达到, stop795_to_stop4frames_s)
        """
        start = monotonic()
        limit = start + timeout_sec
        last_seen_copy = dict(self.last_seen)  # 取当前快照
        # 初始化为 since_ts（停795时刻），若之后再出现，则更新时间
        last_seen = {fid: last_seen_copy.get(fid, since_ts) for fid in CONFIG["FOUR_IDS"]}

        while monotonic() < limit:
            # 查看当前 last_seen
            all_quiet = True
            mx = 0.0
            for fid in CONFIG["FOUR_IDS"]:
                ts = self.last_seen.get(fid, last_seen.get(fid, since_ts))
                last_seen[fid] = ts
                mx = max(mx, ts)
            # “最后一次看到”的最大时刻到现在是否已经静默 >= silence_sec
            silent_ok = (monotonic() - mx) >= silence_sec
            if silent_ok:
                # 统计为 停795 → 最后一帧停止的时间差（以“最后一次出现”到停795的差值）
                # 定义：用 max(last_seen) 与 since_ts 的差值
                stop4_last_ts = mx
                delta = max(0.0, stop4_last_ts - since_ts)
                return True, delta
            sleep_s(0.2)
        # 超时
        # 给出当前能算到的 delta（作为参考）
        mx = 0.0
        for fid in CONFIG["FOUR_IDS"]:
            mx = max(mx, self.last_seen.get(fid, since_ts))
        delta = max(0.0, mx - since_ts)
        return False, delta


# =========================
# 5) Ping & SSH
# =========================

def is_windows() -> bool:
    return platform.system().lower().startswith("win")

def ping_once(ip: str, timeout_sec: float) -> bool:
    if is_windows():
        # -n 1: 发1个包；-w 超时（毫秒）
        cmd = f'ping -n 1 -w {int(timeout_sec*1000)} {ip}'
    else:
        # -c 1: 发1个包；-W 超时（秒）
        cmd = f'ping -c 1 -W {int(timeout_sec)} {ip}'
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def ping_until(ip: str, per_try_timeout: float, budget_sec: float) -> Tuple[bool, int, Optional[float]]:
    """返回 (是否成功, 尝试次数, 首次成功耗时s)"""
    start = monotonic()
    tries = 0
    first_ok: Optional[float] = None
    while monotonic() - start < budget_sec:
        tries += 1
        ok = ping_once(ip, per_try_timeout)
        if ok:
            first_ok = monotonic() - start
            return True, tries, first_ok
        sleep_s(0.2)
    return False, tries, first_ok

def ssh_connect_with_retry(cfg: Dict[str, Any], budget_sec: float,
                           backoff_base: float, factor: float, jitter_sec: float,
                           logger=lambda x: None) -> Optional[paramiko.SSHClient]:
    if paramiko is None:
        raise RuntimeError("未安装 paramiko，请先 pip install paramiko")

    start = monotonic()
    delay = max(0.5, backoff_base)
    while monotonic() - start < budget_sec:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                cfg["host"],
                port=cfg.get("port", 22),
                username=cfg.get("user"),
                password=cfg.get("password"),
                timeout=10.0,
                banner_timeout=15.0,
                auth_timeout=15.0,
            )
            return ssh
        except Exception as e:
            remain = budget_sec - (monotonic() - start)
            logger(f"[SSH] 连接 {cfg['host']} 失败：{e}，剩余预算 {remain:.1f}s，{delay:.1f}s 后重试")
            sleep_s(delay + random.uniform(0, max(0.0, jitter_sec)))
            delay = min(delay * factor, 60.0)
    return None

def ssh_ping_targets(ssh: paramiko.SSHClient, targets: List[str], per_try_timeout: float, per_ip_budget: float) -> Tuple[bool, Dict[str, Any]]:
    """
    在模组内对 targets 逐个尝试，直到首通或预算耗尽。
    返回： (是否全部成功, details)
    details: {"first_ok_map":{ip:sec}, "attempts_map":{ip:cnt}, "fail_list":[ip,...]}
    """
    # Linux 上：ping -c 1 -W timeout
    details = {"first_ok_map": {}, "attempts_map": {}, "fail_list": []}
    all_ok = True
    for ip in targets:
        start = monotonic()
        tries = 0
        first_ok: Optional[float] = None
        ok = False
        while monotonic() - start < per_ip_budget:
            tries += 1
            cmd = f"ping -c 1 -W {int(max(1, per_try_timeout))} {ip}"
            try:
                _, stdout, stderr = ssh.exec_command(cmd, timeout=max(5.0, per_try_timeout+2.0))
                exit_code = stdout.channel.recv_exit_status()
                if exit_code == 0:
                    ok = True
                    first_ok = monotonic() - start
                    break
            except Exception:
                pass
            sleep_s(0.2)
        details["attempts_map"][ip] = tries
        if ok and first_ok is not None:
            details["first_ok_map"][ip] = round(first_ok, 3)
        else:
            details["fail_list"].append(ip)
            all_ok = False
    return all_ok, details


# =========================
# 6) 核心业务：一次“上电→验证→入睡”流程
# =========================

@dataclass
class CycleResult:
    cycle_id: int
    cmd: str                                # "01" 或 "04"
    cmd_ts: datetime
    window_deadline_ts: datetime
    hard_deadline_ts: datetime
    next_01_seen_ts: Optional[datetime] = None
    next_01_delta_s: Optional[float] = None
    window_pass: Optional[bool] = None

    gt300_within_3m: Optional[bool] = None
    gt300_ts: Optional[datetime] = None
    gt400_ts: Optional[datetime] = None
    max_ma_during_online: Optional[float] = None
    sleep_threshold_ma: Optional[float] = None

    send_795_on_ts: Optional[datetime] = None
    send_795_off_ts: Optional[datetime] = None

    # 连通性
    ping_all_pass: Optional[bool] = None
    host_pass: Optional[bool] = None
    main_pass: Optional[bool] = None
    backup_pass: Optional[bool] = None
    host_first_ok_map: Dict[str, float] = field(default_factory=dict)
    main_first_ok_map: Dict[str, float] = field(default_factory=dict)
    backup_first_ok_map: Dict[str, float] = field(default_factory=dict)
    host_attempts_map: Dict[str, int] = field(default_factory=dict)
    main_attempts_map: Dict[str, int] = field(default_factory=dict)
    backup_attempts_map: Dict[str, int] = field(default_factory=dict)
    fail_list: List[str] = field(default_factory=list)  # 汇总失败 IP

    # 入睡
    last_four_frames_seen_ts: Optional[datetime] = None
    stop795_to_stop4frames_s: Optional[float] = None
    sleep_reached_within_5m: Optional[bool] = None
    sleep_judge_reason: Optional[str] = None

    final_verdict: Optional[str] = None      # "PASS"/"FAIL"
    fail_reasons: List[str] = field(default_factory=list)
    notes: Dict[str, Any] = field(default_factory=dict)

    def to_csv_row(self) -> List[Any]:
        return [
            fmt_ts(self.cmd_ts), self.cycle_id, self.cmd,
            fmt_ts(self.send_795_on_ts), fmt_ts(self.send_795_off_ts),
            round(self.next_01_delta_s, 3) if self.next_01_delta_s is not None else None,
            self.window_pass,
            self.gt300_within_3m, fmt_ts(self.gt300_ts), fmt_ts(self.gt400_ts),
            self.max_ma_during_online,
            self.ping_all_pass, self.host_pass, self.main_pass, self.backup_pass,
            round(self.stop795_to_stop4frames_s, 3) if self.stop795_to_stop4frames_s is not None else None,
            self.sleep_reached_within_5m, self.sleep_judge_reason,
            self.final_verdict, json.dumps(self.fail_reasons, ensure_ascii=False),
            json.dumps(self.notes, ensure_ascii=False)
        ]


CSV_HEADER = [
    "cmd_ts","cycle","cmd",
    "send_795_on_ts","send_795_off_ts",
    "next_01_delta_s","window_pass",
    "gt300_within_3m","gt300_ts","gt400_ts",
    "max_ma_during_online",
    "ping_all_pass","host_pass","main_pass","backup_pass",
    "stop795_to_stop4frames_s",
    "sleep_reached_within_5m","sleep_judge_reason",
    "final_verdict","fail_reasons","notes"
]


class Supervisor:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.print = lambda m: log_print(cfg["LOG_PRINT"], m)
        # Power
        ps_cfg = cfg["POWER_SOURCE"]
        mode = ps_cfg["mode"]
        if mode == "file":
            self.power = FileTailPowerSource(...)
        elif mode == "scpi":
            self.power = ScpiPowerSource(...)
        elif mode == "serial":
            s = ps_cfg["serial"]
            self.power = SerialPowerSource(s["port"], s["baud"], s["timeout"],
                                           ps_cfg["poll_interval_sec"], logger=self.print)
        else:
            self.power = DummyPowerSource(...)

        self.canmon = CANMonitor(cfg, logger=self.print)

        # 795 控制
        self._send795_stop_evt = threading.Event()
        self._send795_thread: Optional[threading.Thread] = None

        # 状态
        self.cycle_id = 0
        self.active_cycle: Optional[CycleResult] = None
        self._lock = threading.Lock()

        # 日志文件
        self.csv_path = cfg["CSV_PATH"]
        self.jsonl_path = cfg["JSONL_PATH"]
        self._init_csv()

        # 四报文最后出现的 wall time（用于填表）
        self._four_last_wall_ts: Optional[datetime] = None

        # 窗口跟踪：最近一次上电 01 的时间
        self._last_01_ts: Optional[datetime] = None

    def _init_csv(self):
        need_header = not os.path.exists(self.csv_path)
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if need_header:
                w.writerow(CSV_HEADER)

    def _write_csv(self, res: CycleResult):
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(res.to_csv_row())

    def _write_jsonl(self, obj: Dict[str, Any]):
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def start(self):
        self.print("[SUP] 启动 Power/CAN...")
        self.power.start()
        self.canmon.start()
        threading.Thread(target=self._loop, name="SUPLoop", daemon=True).start()
        self.print("[SUP] 主循环启动")

    def stop(self):
        self._stop_795()
        self.canmon.stop()
        self.power.stop()

    # ============ 795 控制 ============
    def _start_795(self):
        if self._send795_thread and self._send795_thread.is_alive():
            return
        self._send795_stop_evt.clear()
        self._send795_thread = threading.Thread(
            target=self.canmon.send_795_loop,
            args=(
                self._send795_stop_evt,
                self.cfg["SEND_795_PERIOD_MS"],
                self.cfg["SEND_795_DATA"],
                self.cfg["SEND_795_EXTENDED"]
            ),
            name="Send795", daemon=True
        )
        self._send795_thread.start()

    def _stop_795(self):
        self._send795_stop_evt.set()
        if self._send795_thread:
            self._send795_thread.join(timeout=2.0)

    # ============ 主循环 ============
    def _loop(self):
        while True:
            try:
                msg = self.canmon.msg_q.get(timeout=0.2)
            except queue.Empty:
                # 轮询：更新四报文最后出现的 wall time
                self._update_four_frames_wall_ts()
                self._check_window_hard_deadline()
                continue

            # 处理 CAN 报文
            if msg.arbitration_id == self.cfg["ID_6D0"]:
                data = list(msg.data)[:8]
                code = data[0] if data else 0x00
                if code == self.cfg["ID_6D0_UP_DATA"][0]:
                    self._on_6d0_up()
                elif code == self.cfg["ID_6D0_DOWN_DATA"][0]:
                    self._on_6d0_down()
            # 更新四报文的 wall time（用于 UI/日志记录）
            if msg.arbitration_id in self.cfg["FOUR_IDS"]:
                self._four_last_wall_ts = now_dt()

    def _update_four_frames_wall_ts(self):
        # 空转时也写一下 wall ts（保持活性）
        pass

    def _on_6d0_up(self):
        with self._lock:
            self.cycle_id += 1
            nowt = now_dt()
            self._last_01_ts = nowt
            res = CycleResult(
                cycle_id=self.cycle_id,
                cmd="01",
                cmd_ts=nowt,
                window_deadline_ts=nowt + timedelta(minutes=self.cfg["WINDOW_SLA_MIN"]),
                hard_deadline_ts=nowt + timedelta(minutes=self.cfg["WINDOW_HARD_MIN"]),
                sleep_threshold_ma=float(self.cfg["SLEEP_THRESHOLD_MA"])
            )
            self.active_cycle = res
            self.print(f"[6D0] #{res.cycle_id} 上电 01 收到 @ {fmt_ts(nowt)} -> 开始新一轮")
        # 异步跑本轮动作
        threading.Thread(target=self._run_cycle_actions, args=(res,), name=f"Cycle-{res.cycle_id}", daemon=True).start()

    def _on_6d0_down(self):
        # 记录为旁证；窗口 PASS 以“下一次 01”判
        self.print(f"[6D0] 下电 04 收到（旁证用）")

    def _check_window_hard_deadline(self):
        with self._lock:
            res = self.active_cycle
        if not res:
            return
        if now_dt() >= res.hard_deadline_ts and res.next_01_seen_ts is None:
            # 超过70min仍未见下一次01，窗口项 FAIL 并强制重置
            res.window_pass = False
            res.fail_reasons.append("WINDOW>70min: 未见下一次 6D0=01")
            self._finalize_cycle(res, force=True)
            self.print(f"[WIN] #{res.cycle_id} 窗口 >70min，强制结束并重置等待下一轮")

    def notify_next_01(self, ts: datetime):
        with self._lock:
            res = self.active_cycle
        if not res:
            return
        if res.next_01_seen_ts is None:
            res.next_01_seen_ts = ts
            res.next_01_delta_s = (ts - res.cmd_ts).total_seconds()
            res.window_pass = (res.next_01_delta_s <= self.cfg["WINDOW_SLA_MIN"] * 60)
            self.print(f"[WIN] #{res.cycle_id} 下一次 01 到来：{res.next_01_delta_s:.1f}s，window_pass={res.window_pass}")

    # ============ 本轮动作主流程 ============
    def _run_cycle_actions(self, res: CycleResult):
        # 1) 电流监测（3分钟内是否 >300mA；首次 >400mA 的时刻）
        t0 = monotonic()
        gt300_recorded = False
        max_ma = 0.0
        gt400_hit = False
        gt300_limit = t0 + 180.0 if self.cfg["GT300_WITHIN_3M"] else t0  # 是否启用
        self.print(f"[PWR] 监测开始：3分钟内>300mA记录={'ON' if self.cfg['GT300_WITHIN_3M'] else 'OFF'}，>400mA触发连通性")

        # 在窗口内执行动作（<=60min 完成）
        window_deadline = res.window_deadline_ts

        # 循环等待直到 >400mA 或窗口到期
        while now_dt() < window_deadline:
            ma = self.power.last_ma()
            if ma is not None:
                max_ma = max(max_ma, ma)
                if (not gt300_recorded) and (monotonic() <= gt300_limit) and (ma > self.cfg["GT300_THRESHOLD_MA"]):
                    res.gt300_within_3m = True
                    res.gt300_ts = now_dt()
                    gt300_recorded = True
                    self.print(f"[PWR] 上电3分钟内曾>300mA：{ma:.1f}mA")
                if (not gt400_hit) and (ma > self.cfg["WAKE_THRESHOLD_MA"]):
                    # 首次超过 400mA，进入验证阶段
                    res.gt400_ts = now_dt()
                    gt400_hit = True
                    self.print(f"[PWR] 首次>400mA @ {fmt_ts(res.gt400_ts)}，开始发送 0x795 + 连通性验证")
                    break
            sleep_s(self.cfg["POWER_SOURCE"]["poll_interval_sec"])

        res.max_ma_during_online = round(max_ma, 1)

        if not gt400_hit:
            res.fail_reasons.append(f">400mA 未达成（窗口≤60min内未见充分唤醒）")
            return self._finalize_cycle(res)

        # 2) 发 0x795（保持唤醒）
        res.send_795_on_ts = now_dt()
        self._start_795()

        # 3) 并发 Ping（Host + Main + Backup）
        ping_all_pass, host_det, main_det, backup_det = self._do_connectivity(res)

        res.host_pass = host_det["pass"]
        res.main_pass = main_det["pass"]
        res.backup_pass = backup_det["pass"]
        res.ping_all_pass = (res.host_pass and res.main_pass and res.backup_pass)

        res.host_first_ok_map = host_det["first_ok_map"]
        res.main_first_ok_map = main_det["first_ok_map"]
        res.backup_first_ok_map = backup_det["first_ok_map"]

        res.host_attempts_map = host_det["attempts_map"]
        res.main_attempts_map = main_det["attempts_map"]
        res.backup_attempts_map = backup_det["attempts_map"]

        if not res.ping_all_pass:
            # 汇总失败 IP
            res.fail_list = host_det["fail_list"] + main_det["fail_list"] + backup_det["fail_list"]
            res.fail_reasons.append(f"Ping 未全通过：失败 {res.fail_list}")
            # 结束发 795
            self._stop_795()
            res.send_795_off_ts = now_dt()
            return self._finalize_cycle(res)

        # 4) 全部 Ping 通过 → 立即停 795，并等待休眠
        self._stop_795()
        res.send_795_off_ts = now_dt()
        stop795_mono = monotonic()
        # 等四报文停止（静默 S 秒），并在 5 分钟内电流下降到休眠阈值
        ok_frames, delta = self.canmon.wait_four_ids_stopped(since_ts=stop795_mono,
                                                             silence_sec=self.cfg["SLEEP_SILENCE_SEC"],
                                                             timeout_sec=self.cfg["SLEEP_REACH_TIMEOUT_SEC"])
        res.stop795_to_stop4frames_s = round(delta, 3)
        res.last_four_frames_seen_ts = now_dt()

        # 休眠电流校验（≤5 分钟）
        sleep_ok = False
        sleep_reason = []
        end_time = monotonic() + self.cfg["SLEEP_REACH_TIMEOUT_SEC"]
        while monotonic() < end_time:
            ma = self.power.last_ma() or 0.0
            if ma <= self.cfg["SLEEP_THRESHOLD_MA"]:
                sleep_ok = True
                sleep_reason.append(f"电流≤阈值({self.cfg['SLEEP_THRESHOLD_MA']}mA)：{ma:.1f}mA")
                break
            sleep_s(0.5)

        if not ok_frames:
            sleep_reason.append(f"四报文未在静默窗口 {self.cfg['SLEEP_SILENCE_SEC']}s 内停止")
        else:
            sleep_reason.append(f"四报文停止耗时={res.stop795_to_stop4frames_s}s")

        res.sleep_reached_within_5m = (ok_frames and sleep_ok)
        res.sleep_judge_reason = " | ".join(sleep_reason)

        if not res.sleep_reached_within_5m:
            res.fail_reasons.append("停795后5分钟内未达休眠（电流/报文不满足）")
            return self._finalize_cycle(res)

        # 5) 等待下一次 6D0=01 来判窗口项（或超 70min 强制终止）
        #   注：窗口项的 PASS 将在 notify_next_01() 被设置，若到 hard_deadline 仍未到来会在心跳里 FAIL。
        #   此处先做一次保存，等待窗口结束后再补 final verdict。
        self._finalize_cycle(res, partial=True)

    def _do_connectivity(self, res: CycleResult) -> Tuple[bool, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        cfg = self.cfg
        # Host 并发 ping
        host_targets = cfg["PING_HOST_TARGETS"]
        per_try = cfg["PING_PER_IP_TIMEOUT_SEC"]
        budget = cfg["PING_PER_IP_BUDGET_SEC"]
        par = max(1, int(cfg["PING_PARALLELISM"]))

        host_det = {"pass": True, "first_ok_map": {}, "attempts_map": {}, "fail_list": []}
        q_targets = queue.Queue()
        for ip in host_targets:
            q_targets.put(ip)

        lock = threading.Lock()

        def worker_host():
            while True:
                try:
                    ip = q_targets.get_nowait()
                except queue.Empty:
                    break
                ok, tries, first_ok = ping_until(ip, per_try, budget)
                with lock:
                    host_det["attempts_map"][ip] = tries
                    if ok and first_ok is not None:
                        host_det["first_ok_map"][ip] = round(first_ok, 3)
                    else:
                        host_det["fail_list"].append(ip)
                        host_det["pass"] = False

        threads = [threading.Thread(target=worker_host, daemon=True) for _ in range(par)]
        for t in threads: t.start()
        for t in threads: t.join()

        # SSH 连接（Main / Backup）
        ssh_budget = cfg["SSH_CONNECT_BUDGET_SEC"]
        ssh_base = cfg["SSH_BACKOFF_BASE_SEC"]
        ssh_factor = cfg["SSH_BACKOFF_FACTOR"]
        ssh_jitter = cfg["SSH_BACKOFF_JITTER_SEC"]

        main_det = {"pass": True, "first_ok_map": {}, "attempts_map": {}, "fail_list": []}
        backup_det = {"pass": True, "first_ok_map": {}, "attempts_map": {}, "fail_list": []}

        # Main
        ssh_main = ssh_connect_with_retry(cfg["SSH_MAIN"], ssh_budget, ssh_base, ssh_factor, ssh_jitter, logger=self.print)
        if not ssh_main:
            main_det["pass"] = False
            main_det["fail_list"] = cfg["PING_MAIN_TARGETS"][:]  # 全部未测即视为失败
        else:
            ok, det = ssh_ping_targets(ssh_main, cfg["PING_MAIN_TARGETS"], per_try, budget)
            main_det["pass"] = ok
            main_det.update(det)
            try:
                ssh_main.close()
            except Exception:
                pass

        # Backup
        ssh_backup = ssh_connect_with_retry(cfg["SSH_BACKUP"], ssh_budget, ssh_base, ssh_factor, ssh_jitter, logger=self.print)
        if not ssh_backup:
            backup_det["pass"] = False
            backup_det["fail_list"] = cfg["PING_BACKUP_TARGETS"][:]
        else:
            ok, det = ssh_ping_targets(ssh_backup, cfg["PING_BACKUP_TARGETS"], per_try, budget)
            backup_det["pass"] = ok
            backup_det.update(det)
            try:
                ssh_backup.close()
            except Exception:
                pass

        all_ok = host_det["pass"] and main_det["pass"] and backup_det["pass"]
        return all_ok, host_det, main_det, backup_det

    # ============ 结束与落盘 ============
    def _finalize_cycle(self, res: CycleResult, partial: bool=False, force: bool=False):
        # 若下一次 01 已到来，则更新窗口项
        if res.next_01_seen_ts is None and self._last_01_ts and self._last_01_ts > res.cmd_ts:
            self.notify_next_01(self._last_01_ts)

        # 判定最终结论
        verdict = "PASS"
        reasons = []

        # 1) 窗口项
        if res.window_pass is False:
            verdict = "FAIL"
            reasons.append("WINDOW_FAIL")

        # 2) 3分钟>300mA（记录项，不直接拉低结论；若需要可加开关）
        # if CONFIG["GT300_WITHIN_3M"] and res.gt300_within_3m is not True:
        #     verdict = "FAIL"; reasons.append("GT300_WITHIN_3M_FAIL")

        # 3) 连通性
        if res.ping_all_pass is False:
            verdict = "FAIL"
            reasons.append("PING_FAIL")

        # 4) 休眠
        if res.sleep_reached_within_5m is False:
            verdict = "FAIL"
            reasons.append("SLEEP_FAIL")

        # 若 partial 且尚未到窗口结论，不立即写 PASS/FAIL，仅落地一次中间态
        if partial and (res.window_pass is None):
            res.final_verdict = "PENDING"
        else:
            res.final_verdict = verdict

        res.fail_reasons.extend(reasons)

        # 记录 notes（详细 map）
        notes = {
            "host_first_ok_map": res.host_first_ok_map,
            "main_first_ok_map": res.main_first_ok_map,
            "backup_first_ok_map": res.backup_first_ok_map,
            "host_attempts_map": res.host_attempts_map,
            "main_attempts_map": res.main_attempts_map,
            "backup_attempts_map": res.backup_attempts_map,
        }
        res.notes.update(notes)

        # 落盘
        self._write_csv(res)
        self._write_jsonl({**asdict(res), "ts": fmt_ts(now_dt())})

        # 如果是强制（>70min）或已最终态，则清理 active_cycle
        if force or res.final_verdict in ("PASS", "FAIL"):
            with self._lock:
                # 进入空闲，等待下一轮 6D0
                self.active_cycle = None

        # 控制台输出摘要
        self.print(f"[END] #{res.cycle_id} verdict={res.final_verdict} window_pass={res.window_pass} ping_all_pass={res.ping_all_pass} sleep_ok={res.sleep_reached_within_5m}")
        if res.fail_reasons:
            self.print(f"[END] 失败原因：{res.fail_reasons}")



# =========================
# 7) 入口
# =========================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="TBOX 6D0→795→Ping→Sleep 监测脚本（冻结规范 v1.0）")
    parser.add_argument("--config", type=str, help="外部配置文件（.json/.yaml）", default=None)
    args = parser.parse_args()

    cfg = json.loads(json.dumps(CONFIG))  # 深拷贝
    if args.config:
        ext = load_config_from_file(args.config)
        merge_dict(cfg, ext)

    sup = Supervisor(cfg)
    sup.start()

    print("[RUN] 运行中，Ctrl+C 退出")
    try:
        # 附加：监听 6D0=01 的到来用于窗口判定（主动从 CAN 线程中设置）
        # 这里简单实现：定时检查 active_cycle 的 next_01 是否已自然发生
        while True:
            with sup._lock:
                res = sup.active_cycle
            if res and res.next_01_seen_ts is None:
                # 如果 CAN 线程又收到了 01，会走 _on_6d0_up 新建下一轮，此处也会顺带调用 notify_next_01
                pass
            sleep_s(0.5)
    except KeyboardInterrupt:
        print("\n[STOP] 收到中断，退出...")
    finally:
        sup.stop()


if __name__ == "__main__":
    main()
