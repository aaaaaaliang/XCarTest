#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 专用
TBOX 上/下电（6D0）→ 唤醒验证（>400mA 发 0x795 + 并发 Ping）→ 停 795 等休眠的全流程监测脚本
—— 冻结规范 v1.0 实现版（CAN: Vector+CANoe；电源：串口SCPI；SSH 密码: oelinux123）
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

# 可选 YAML 配置支持（自动加载同目录 config.yaml，如不存在就用内置 CONFIG）
try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# 第三方依赖：python-can / paramiko
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
    "GT300_WITHIN_3M": True,                # 是否记录上电后3分钟内>300mA（记录项）
    "GT300_THRESHOLD_MA": 300,
    "WAKE_THRESHOLD_MA": 400,               # >400mA 视为充分唤醒，开始发795+Ping
    "SLEEP_THRESHOLD_MA": 120,              # 休眠电流阈值（可按你的实际调整）
    "SLEEP_SILENCE_SEC": 3.0,               # 判定“四报文停止”的静默窗口（秒）
    "SLEEP_REACH_TIMEOUT_SEC": 5 * 60,      # 停795后，5分钟内需达到休眠

    # ---- 6D0/795 & 四报文 ----
    "ID_6D0": 0x6D0,
    "ID_795": 0x795,
    "FOUR_IDS": [0x5A0, 0x603, 0x6C0, 0x6C1],
    "ID_6D0_UP_DATA":   [0x01, 0, 0, 0, 0, 0, 0, 0],  # 上电 01
    "ID_6D0_DOWN_DATA": [0x04, 0, 0, 0, 0, 0, 0, 0],  # 下电 04
    "FRAME_PAYLOAD_LEN": 8,

    # ---- 795 持续发送 ----
    "SEND_795_PERIOD_MS": 100,              # 795 发送周期（毫秒）
    "SEND_795_DATA": [0x00]*8,              # 795 数据（按需要修改）
    "SEND_795_EXTENDED": False,             # 标准帧 False

    # ---- CAN（Windows + Vector + CANoe）----
    "CAN": {
        "bustype": "vector",                # 只支持 vector
        "channel": 0,                       # CANoe 中的通道索引：0 = CAN1, 1 = CAN2 ...
        "bitrate": 500000,
        "app_name": "CANoe",                # ⭐ 指定 CANoe
    },

    # ---- 串口编程电源（Windows COM 口）----
    "POWER_SOURCE": {
        "mode": "serial",                   # 串口专用
        "serial": {
            "port": "COM3",                 # 改成你的端口（如 COM5）
            "baud": 115200,
            "timeout": 1.0,

            # 启动时是否先断电→再上电（OUTP OFF→ON），并可选设定电压/电流
            "power_cycle_on_start": True,
            "voltage_v": 12.0,
            "current_a": 2.0,
            "off_wait_s": 1.0,
            "on_wait_s": 0.5
        },
        "poll_interval_sec": 0.5
    },

    # ---- Ping 目标清单 ----
    "PING_HOST_TARGETS":   ["192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202"],
    "PING_MAIN_TARGETS":   ["192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202", "14.103.165.189"],
    "PING_BACKUP_TARGETS": ["192.168.1.99", "192.168.1.9", "192.168.1.1", "192.168.1.201", "192.168.1.202", "14.103.165.189"],

    # ---- SSH 参数 & 重试策略（密码已固定为 oelinux123）----
    "SSH_MAIN":   {"host": "192.168.1.201", "port": 22, "user": "root", "password": "oelinux123"},
    "SSH_BACKUP": {"host": "192.168.1.202", "port": 22, "user": "root", "password": "oelinux123"},
    "SSH_CONNECT_BUDGET_SEC": 300,          # SSH 连接总预算
    "SSH_BACKOFF_BASE_SEC": 1.2,            # 初始退避
    "SSH_BACKOFF_FACTOR": 1.8,              # 乘数
    "SSH_BACKOFF_JITTER_SEC": 0.8,          # 抖动上限

    # ---- Ping 重试策略 ----
    "PING_PER_IP_TIMEOUT_SEC": 2.0,         # 单次 ping 等待
    "PING_PER_IP_BUDGET_SEC": 60.0,         # 每个 IP 总预算（>400mA 后开始计）
    "PING_PARALLELISM": 8,                  # 并发度

    # ---- 其他 ----
    "NOTES": "按冻结规范 v1.0；支持 optional config.yaml 覆盖（同目录自动加载）。"
}


# =========================
# 2) 工具函数
# =========================

def now_dt() -> datetime:
    return datetime.now()

def fmt_ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
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

def try_load_local_yaml(default_cfg: Dict[str, Any], filename: str = "config.yaml") -> Dict[str, Any]:
    cfg = json.loads(json.dumps(default_cfg))  # 深拷贝
    if os.path.exists(filename):
        try:
            ext = load_config_from_file(filename)
            merge_dict(cfg, ext)
        except Exception as e:
            print(f"[CFG] 加载 {filename} 失败：{e}，将继续使用内置默认值")
    return cfg

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
# 3) 电流读取源（串口）
# =========================

class PowerSource:
    def start(self): ...
    def stop(self): ...
    def last_ma(self) -> Optional[float]: ...
    def snapshot(self) -> Dict[str, Any]: ...

class SerialPowerSource(PowerSource):
    """通过串口 (pyserial) + SCPI 协议读取电流，单位 mA；支持 VOLT/CURR/OUTP ON/OFF。"""
    def __init__(self, port: str = "COM3", baud: int = 115200, timeout: float = 1.0,
                 poll_interval_sec: float = 0.5, logger=lambda x: None):
        import serial
        self.serial_mod = serial
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
            self.ser = self.serial_mod.Serial(port=self.port, baudrate=self.baud, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError(f"[SerialPowerSource] 打开串口失败: {e}")

    def _query(self, cmd: str) -> str:
        """发送命令并返回响应（按行读取，带超时；发送前清空残留缓冲）。"""
        try:
            # 清掉可能的残留输入，避免读到旧数据
            try:
                if self.ser.in_waiting:
                    self.ser.reset_input_buffer()
            except Exception:
                pass

            self.ser.write((cmd + "\r\n").encode())
            self.ser.flush()

            deadline = time.time() + float(self.timeout or 1.0)
            buf = b""
            while time.time() < deadline:
                line = self.ser.readline()  # 读到 \n
                if line:
                    buf += line
                    # 常见设备以 \n 或 \r\n 结束；读到行尾即收
                    if buf.endswith(b"\n") or buf.endswith(b"\r\n"):
                        break
            return buf.decode(errors="ignore").strip()
        except Exception as e:
            self._logger(f"[SerialPowerSource] 命令 {cmd} 失败: {e}")
            return ""

    # 控制命令
    def on(self):
        self._query("OUTP ON")

    def off(self):
        self._query("OUTP OFF")

    def set_voltage(self, v: float):
        self._query(f"VOLT {v}")

    def set_current(self, a: float):
        self._query(f"CURR {a}")

    def measure_current_a(self) -> Optional[float]:
        """返回单位安培的电流（可能为 None）"""
        try:
            resp = self._query("MEAS:CURR?")
            if resp:
                return float(resp)
        except Exception as e:
            self._logger(f"[SerialPowerSource] 读电流失败: {e}")
        return None

    def _run(self):
        while not self._stop.is_set():
            try:
                amps = self.measure_current_a()
                if amps is not None:
                    self._ma = amps * 1000.0
            except self.serial_mod.SerialException:
                # 尝试自动重连
                try:
                    self.ser.close()
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    self.ser = self.serial_mod.Serial(port=self.port, baudrate=self.baud, timeout=self.timeout)
                except Exception as e:
                    self._logger(f"[SerialPowerSource] 串口重连失败：{e}")
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
        return {"mode": "serial", "port": self.port, "baud": self.baud, "last_ma": self._ma}


# =========================
# 4) CAN 监听/发送（Vector + CANoe）
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

    def start(self):
        bus_cfg = self.cfg["CAN"]
        kwargs = {
            "bustype":  bus_cfg.get("bustype", "vector"),
            "channel":  bus_cfg.get("channel", 0),
            "bitrate":  bus_cfg.get("bitrate", 500000)
        }
        if kwargs["bustype"] == "vector":
            app_name = bus_cfg.get("app_name", "CANoe")
            kwargs["app_name"] = app_name

        self.bus = can.interface.Bus(**kwargs)
        self._stop.clear()
        self._th = threading.Thread(target=self._recv_loop, name="CANRecv", daemon=True)
        self._th.start()
        self.logger("[CAN] 接收线程启动 (Vector/CANoe)")

    def _recv_loop(self):
        assert self.bus is not None
        while not self._stop.is_set():
            try:
                msg = self.bus.recv(timeout=0.2)
                if msg is None:
                    continue
                # 记录四报文的最后出现时刻（单调时钟）
                if msg.arbitration_id in self.cfg["FOUR_IDS"]:
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
        dlc = min(len(data), self.cfg["FRAME_PAYLOAD_LEN"])
        msg = can.Message(arbitration_id=self.cfg["ID_795"],
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
        last_seen_copy = dict(self.last_seen)
        last_seen = {fid: last_seen_copy.get(fid, since_ts) for fid in self.cfg["FOUR_IDS"]}

        while monotonic() < limit:
            mx = 0.0
            for fid in self.cfg["FOUR_IDS"]:
                ts = self.last_seen.get(fid, last_seen.get(fid, since_ts))
                last_seen[fid] = ts
                mx = max(mx, ts)
            silent_ok = (monotonic() - mx) >= silence_sec
            if silent_ok:
                delta = max(0.0, mx - since_ts)
                return True, delta
            sleep_s(0.2)

        mx = 0.0
        for fid in self.cfg["FOUR_IDS"]:
            mx = max(mx, self.last_seen.get(fid, since_ts))
        delta = max(0.0, mx - since_ts)
        return False, delta


# =========================
# 5) Ping & SSH（Windows 专用 ping）
# =========================

def ping_once(ip: str, timeout_sec: float) -> bool:
    """Windows 专用：-n 1 -w 毫秒"""
    cmd = f'ping -n 1 -w {int(timeout_sec*1000)} {ip}'
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

def ssh_ping_targets(ssh: paramiko.SSHClient, targets: List[str],
                     per_try_timeout: float, per_ip_budget: float) -> Tuple[bool, Dict[str, Any]]:
    """
    在模组内对 targets 逐个尝试，直到首通或预算耗尽。
    返回： (是否全部成功, details={'first_ok_map':{}, 'attempts_map':{}, 'fail_list':[]})
    """
    details = {"first_ok_map": {}, "attempts_map": {}, "fail_list": []}
    all_ok = True
    for ip in targets:
        start = monotonic()
        tries = 0
        first_ok: Optional[float] = None
        ok = False
        while monotonic() - start < per_ip_budget:
            tries += 1
            cmd = f"ping -c 1 -W {int(max(1, per_try_timeout))} {ip}"  # 目标 Linux 常用
            try:
                _, stdout, _ = ssh.exec_command(cmd, timeout=max(5.0, per_try_timeout+2.0))
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

    final_verdict: Optional[str] = None      # "PASS"/"FAIL"/"PENDING"
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

        # Power（串口）
        ps_cfg = cfg["POWER_SOURCE"]
        if ps_cfg.get("mode") != "serial":
            raise RuntimeError("当前脚本仅支持串口电源（POWER_SOURCE.mode 必须为 'serial'）")
        s = ps_cfg["serial"]
        self.power = SerialPowerSource(
            port=s["port"], baud=s["baud"], timeout=s["timeout"],
            poll_interval_sec=ps_cfg["poll_interval_sec"], logger=self.print
        )

        # CAN (Vector + CANoe)
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

        # 启动即：断电→上电（仅 serial 模式）
        try:
            s = self.cfg["POWER_SOURCE"]["serial"]
            if s.get("power_cycle_on_start", True):
                v = s.get("voltage_v")
                a = s.get("current_a")
                if v is not None:
                    self.power.set_voltage(v)
                if a is not None:
                    self.power.set_current(a)
                self.print("[PSU] 启动前执行：断电→上电")
                self.power.off()
                sleep_s(float(s.get("off_wait_s", 1.0)))
                self.power.on()
                sleep_s(float(s.get("on_wait_s", 0.5)))
        except Exception as e:
            self.print(f"[PSU] 启动上电失败：{e}")

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

    # ============ 新增：给“指定轮次”打下一次 01 标记 ============
    def _mark_next_01_for(self, res: Optional[CycleResult], ts: datetime):
        """在创建新一轮前，先把上一轮的“下一次 01 到来”打上去，并必要时补最终判定。"""
        if res is None or res.next_01_seen_ts is not None:
            return
        res.next_01_seen_ts = ts
        res.next_01_delta_s = (ts - res.cmd_ts).total_seconds()
        res.window_pass = (res.next_01_delta_s <= self.cfg["WINDOW_SLA_MIN"] * 60)
        self.print(f"[WIN] #{res.cycle_id} 下一次 01 到来：{res.next_01_delta_s:.1f}s，window_pass={res.window_pass}")
        # 如果上一轮之前写的是 PENDING，这里顺手补一个最终落盘
        if res.final_verdict == "PENDING":
            self._finalize_cycle(res, partial=False)

    # ============ 主循环 ============
    def _loop(self):
        while True:
            try:
                msg = self.canmon.msg_q.get(timeout=0.2)
            except queue.Empty:
                # 空转时检查窗口强制上限
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

            if msg.arbitration_id in self.cfg["FOUR_IDS"]:
                self._four_last_wall_ts = now_dt()

    def _on_6d0_up(self):
        with self._lock:
            prev = self.active_cycle         # 先抓上一轮
            nowt = now_dt()

            # ✅ 先把“下一次 01 到来”记在上一轮（修复窗口判定归属问题）
            if prev:
                self._mark_next_01_for(prev, nowt)

            # 再创建新一轮
            self.cycle_id += 1
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

        threading.Thread(target=self._run_cycle_actions, args=(res,), name=f"Cycle-{res.cycle_id}", daemon=True).start()

    def _on_6d0_down(self):
        self.print(f"[6D0] 下电 04 收到（旁证）")

    def _check_window_hard_deadline(self):
        with self._lock:
            res = self.active_cycle
        if not res:
            return
        if now_dt() >= res.hard_deadline_ts and res.next_01_seen_ts is None:
            res.window_pass = False
            res.fail_reasons.append("WINDOW>70min: 未见下一次 6D0=01")
            self._finalize_cycle(res, force=True)
            self.print(f"[WIN] #{res.cycle_id} 窗口 >70min，强制结束并重置等待下一轮")

    # ============ 本轮动作 ============
    def _run_cycle_actions(self, res: CycleResult):
        # 1) 电流监测：>300mA 记录；>400mA 触发连通性
        t0 = monotonic()
        gt300_recorded = False
        max_ma = 0.0
        gt400_hit = False
        gt300_limit = t0 + 180.0 if self.cfg["GT300_WITHIN_3M"] else t0
        self.print(f"[PWR] 监测开始：3分钟内>300mA记录={'ON' if self.cfg['GT300_WITHIN_3M'] else 'OFF'}，>400mA触发连通性")

        window_deadline = res.window_deadline_ts

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
                    res.gt400_ts = now_dt()
                    gt400_hit = True
                    self.print(f"[PWR] 首次>400mA @ {fmt_ts(res.gt400_ts)}，开始发送 0x795 + 连通性验证")
                    break
            sleep_s(self.cfg["POWER_SOURCE"]["poll_interval_sec"])

        res.max_ma_during_online = round(max_ma, 1)

        if not gt400_hit:
            res.fail_reasons.append(">400mA 未达成（窗口≤60min内未见充分唤醒）")
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
            res.fail_list = host_det["fail_list"] + main_det["fail_list"] + backup_det["fail_list"]
            res.fail_reasons.append(f"Ping 未全通过：失败 {res.fail_list}")
            self._stop_795()
            res.send_795_off_ts = now_dt()
            return self._finalize_cycle(res)

        # 4) 全部 Ping 通过 → 立即停 795，并等待休眠
        self._stop_795()
        res.send_795_off_ts = now_dt()
        stop795_mono = monotonic()

        ok_frames, delta = self.canmon.wait_four_ids_stopped(
            since_ts=stop795_mono,
            silence_sec=self.cfg["SLEEP_SILENCE_SEC"],
            timeout_sec=self.cfg["SLEEP_REACH_TIMEOUT_SEC"]
        )
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
        self._finalize_cycle(res, partial=True)

    def _do_connectivity(self, res: CycleResult) -> Tuple[bool, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        cfg = self.cfg
        # Host 并发 ping（Windows 本机）
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

        # SSH 连接（Main / Backup，远端 Linux）
        ssh_budget = cfg["SSH_CONNECT_BUDGET_SEC"]
        ssh_base = cfg["SSH_BACKOFF_BASE_SEC"]
        ssh_factor = cfg["SSH_BACKOFF_FACTOR"]
        ssh_jitter = cfg["SSH_BACKOFF_JITTER_SEC"]

        main_det = {"pass": True, "first_ok_map": {}, "attempts_map": {}, "fail_list": []}
        backup_det = {"pass": True, "first_ok_map": {}, "attempts_map": {}, "fail_list": []}

        ssh_main = ssh_connect_with_retry(cfg["SSH_MAIN"], ssh_budget, ssh_base, ssh_factor, ssh_jitter, logger=self.print)
        if not ssh_main:
            main_det["pass"] = False
            main_det["fail_list"] = cfg["PING_MAIN_TARGETS"][:]
        else:
            ok, det = ssh_ping_targets(ssh_main, cfg["PING_MAIN_TARGETS"], per_try, budget)
            main_det["pass"] = ok
            main_det.update(det)
            try:
                ssh_main.close()
            except Exception:
                pass

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
        # 判定最终结论
        verdict = "PASS"
        reasons = []

        if res.window_pass is False:
            verdict = "FAIL"
            reasons.append("WINDOW_FAIL")

        if res.ping_all_pass is False:
            verdict = "FAIL"
            reasons.append("PING_FAIL")

        if res.sleep_reached_within_5m is False:
            verdict = "FAIL"
            reasons.append("SLEEP_FAIL")

        if partial and (res.window_pass is None):
            res.final_verdict = "PENDING"
        else:
            res.final_verdict = verdict

        res.fail_reasons.extend(reasons)

        # 记录 notes
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
                if self.active_cycle is res:
                    self.active_cycle = None

        # 控制台摘要
        self.print(f"[END] #{res.cycle_id} verdict={res.final_verdict} window_pass={res.window_pass} ping_all_pass={res.ping_all_pass} sleep_ok={res.sleep_reached_within_5m}")
        if res.fail_reasons:
            self.print(f"[END] 失败原因：{res.fail_reasons}")


# =========================
# 7) 入口（自动尝试加载同目录 config.yaml）
# =========================

def main():
    cfg = try_load_local_yaml(CONFIG, filename="config.yaml")
    sup = Supervisor(cfg)
    sup.start()

    print("[RUN] 运行中，Ctrl+C 退出")
    try:
        while True:
            # 轮询仅为保持主线程活性；窗口判定由 _on_6d0_up 和 _check_window_hard_deadline 驱动
            sleep_s(0.5)
    except KeyboardInterrupt:
        print("\n[STOP] 收到中断，退出...")
    finally:
        sup.stop()


if __name__ == "__main__":
    main()
