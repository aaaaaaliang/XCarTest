#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBOX 休眠/唤醒循环验证（纯 CAN + Ping + 可选电源电流判据）
- 首次上电：可选是否发送 0x795（FIRST_BOOT_SEND_WAKE）
- 无论是否发 0x795，电流 > 阈值(默认400mA) 就做主机+主/副模组的全量 ping
- 休眠判定：监听 OUT_TX 在静默窗口内彻底静默
- 休眠后唤醒：固定等待 or 随机等待（可配置）

依赖：
  pip install python-can paramiko
并且你的库中已提供：
  from car_test import OwonPSU, CanBus, SSHClient, ping_local
"""

from __future__ import annotations

import os
import csv
import json
import time
import random
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set

import can  # 仅用于构造 Message；底层总线由 car_test.CanBus 打开
from car_test import OwonPSU, CanBus, SSHClient, ping_local

# ========================= 配置区（按现场改） =========================
# —— 首次上电策略 ——
FIRST_BOOT_SEND_WAKE: bool = True          # True=上电立刻发0x795保持唤醒；False=不发，让TBOX自管
FIRST_BOOT_STOP_WAKE_AFTER_PINGS: bool = True  # 首次上电如果发了0x795，做完ping后是否立即停发

# —— 电源/电流判据（可选） ——
USE_PSU: bool = True                       # 是否通过电源读取电流门槛；False=无电源接口，跳过门槛判据
PSU_COM: str = "COM3"                      # 你的电源串口
PSU_BAUD: int = 115200
PSU_VOLT_SET: float = 12.0                 # 需要的话可以上电；不想脚本管上电就把 PSU_SET_OUTPUT=False
PSU_CURR_LIMIT_A: float = 3.0
PSU_SET_OUTPUT: bool = False               # True=脚本控制上电+设压流；False=只读电流，不改电源状态
POWER_WAKE_THRESHOLD_MA: float = 400.0     # 电流门槛（mA）
POWER_HOLD_MS: int = 600                   # 电流连续达标判定保持时长
POWER_TIMEOUT_S: float = 180.0             # 等待电流达标的最长时间（首次/唤醒）

# —— CAN 接口（遵循你的 CanBus 入参） ——
CAN_INTERFACE: str = "vector"              # vector / pcan / kvaser / socketcan ...
CAN_CHANNEL: int = 0                       # Vector: 0/1/...；socketcan 用 "can0" 需改 CanBus 实现
CAN_BITRATE: int = 500000                  # 非 vector 时需要；vector 由驱动配置
CAN_APP_NAME: str = "CANoe"

# —— 唤醒报文 0x795 ——
WAKE_ID: int = 0x795
WAKE_PAYLOAD_HEX: str = "0000000000000000"
WAKE_PERIOD_MS: int = 100                  # 周期（ms）

# —— 用于判定“休眠”的周期外发 ID（字符串或整数皆可） ——
OUT_TX_IDS: List[int | str] = ["0x5A0", "0x603", "0x6C0", "0x6C1"]

# —— 休眠判据 ——
SLEEP_SILENCE_S: float = 3.0               # 静默窗口（建议≥3×最大外发周期）
SLEEP_MAX_WAIT_S: float = 180.0            # 最多等这么久还没静默则 TIMEOUT
SLEEP_PREDRAIN_S: float = 0.2              # 等待前预排空接收队列



# WAKE_RANDOM_ENABLED: bool = False
# WAKE_FIXED_WAIT_S: float = 3600.0   # 1 小时
# —— 休眠后唤醒等待（固定 or 随机） ——
WAKE_RANDOM_ENABLED: bool = True           # True=用随机等待；False=固定等待
WAKE_FIXED_WAIT_S: float = 60.0            # 固定等待秒数
WAKE_MIN_WAIT_S: float = 60.0              # 随机等待下限
WAKE_MAX_WAIT_S: float = 300.0             # 随机等待上限

# —— Ping 目标 ——
# 主机要 ping：
HOST_PROBE_IPS: List[str] = [
    "192.168.1.99", "192.168.1.9", "192.168.1.1",
    "192.168.1.201", "192.168.1.202", "14.103.165.189"
]
# 模组内部也要 ping 同样一批（含自身地址，允许）
MODULE_PROBE_IPS: List[str] = HOST_PROBE_IPS.copy()

# —— SSH 目标（主/副模组；留空则不做该路 SSH） ——
SSH_MAIN: Optional[str] = "192.168.1.201"  # 主模组 IP；无则设 None
SSH_BACKUP: Optional[str] = "192.168.1.202" # 副模组 IP；无则设 None
SSH_USER: str = os.getenv("SSH_USER", "root")
SSH_PASS: Optional[str] = os.getenv("SSH_PASSWORD", "oelinux123") if os.getenv("SSH_PKEY") is None else None
SSH_KEY:  Optional[str] = os.getenv("SSH_PKEY", None)
SSH_PORT: int = int(os.getenv("SSH_PORT", "22"))

# —— Ping 节奏 ——
PING_TIMEOUT_S: float = 30.0               # 首次打通最长等待
PING_INTERVAL_MS: int = 800
PING_FINAL_TRIES: int = 3
PING_FINAL_TIMEOUT_MS: int = 1200

# —— 循环次数 ——
CYCLES: int = 100

# —— 日志目录 ——
LOG_DIR: str = "logs"
# ====================================================================


# -------------------- 小工具：ID 解析与日志 --------------------
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
                "host_all_final_ok","main_ok","backup_ok",
                "stop_to_last_seen_s","sleep_status","sleep_total_wait_s",
                "first_out_tx_seen_ts","notes"
            ])

def _append_summary(path: str, row: List[Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# -------------------- 795 周期发送（线程实现，通用） --------------------
class PeriodicSender:
    def __init__(self, bus: can.BusABC, arb_id: int, data: bytes, period_s: float):
        self.bus = bus
        self.id = arb_id
        self.data = data
        self.per = max(0.01, period_s)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._th.start()
        return self

    def _run(self):
        msg = can.Message(arbitration_id=self.id, data=self.data, is_extended_id=False)
        while not self._stop.is_set():
            try:
                self.bus.send(msg)
            except Exception:
                pass
            self._stop.wait(self.per)

    def stop(self):
        self._stop.set()
        try:
            self._th.join(timeout=1.0)
        except Exception:
            pass


# -------------------- CAN 监听工具 --------------------
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
                      pre_drain_s: float = 0.2) -> Dict[str, Any]:
    """从 stop_time 开始，等待 OUT_TX_IDS 静默 >= silence_window_s。"""
    _predrain(bus, pre_drain_s)

    last_seen: Optional[float] = None
    seen_counter: Dict[int, int] = {}
    start = time.time()

    while True:
        now = time.time()
        if now - start > max_wait_s:
            return {
                "status": "TIMEOUT",
                "last_seen": last_seen,
                "silence_window_s": silence_window_s,
                "seen_counter": {hex(k): v for k, v in seen_counter.items()},
                "elapsed_from_stop_s": now - stop_time
            }

        frame = bus.recv(timeout=0.05)
        if frame is None:
            # 判定静默：距离最后一次见到 OUT_TX >= 窗口
            if last_seen is not None and (now - last_seen) >= silence_window_s and now >= stop_time:
                return {
                    "status": "ASLEEP",
                    "last_seen": last_seen,
                    "silence_window_s": silence_window_s,
                    "seen_counter": {hex(k): v for k, v in seen_counter.items() },
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
            event_log({"evt": "OUT_TX", "id": hex(arb), "t": round(ts - stop_time, 3)})


# -------------------- Ping 流程（主机 & 远端） --------------------
def wait_first_ok_local(ip: str, total_timeout_s: float, interval_ms: int) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    start = time.time()
    attempts: List[Dict[str, Any]] = []
    while True:
        ok, out = ping_local(ip, count=1, timeout=max(1, int(interval_ms/1000)))
        attempts.append({"t": round(time.time() - start, 3), "ok": ok, "out": out[-500:]})
        if ok:
            return time.time() - start, attempts
        if time.time() - start >= total_timeout_s:
            return None, attempts
        time.sleep(max(0.0, interval_ms/1000.0))

def final_n_local(ip: str, tries: int, timeout_ms: int) -> Tuple[bool, List[str]]:
    outs: List[str] = []
    ok = False
    for _ in range(max(1, tries)):
        ok1, out = ping_local(ip, count=1, timeout=max(1, int(timeout_ms/1000)))
        ok = ok1 or ok
        outs.append(out[-500:])
    return ok, outs

def wait_first_ok_ssh(host: str) -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    if not host:
        return res
    try:
        ssh = SSHClient(host, port=SSH_PORT, user=SSH_USER, password=SSH_PASS, pkey_path=SSH_KEY, timeout=15)
        ssh.connect()
        try:
            for ip in MODULE_PROBE_IPS:
                # 轮询直到首次打通或超时
                start = time.time()
                attempts: List[Dict[str, Any]] = []
                while True:
                    ok = ssh.ping(ip, count=1, timeout=max(1, int(PING_INTERVAL_MS/1000)))
                    attempts.append({"t": round(time.time() - start, 3), "ok": ok, "out": f"ssh_ping {ip} -> {ok}"})
                    if ok:
                        t_first = time.time() - start
                        break
                    if time.time() - start >= PING_TIMEOUT_S:
                        t_first = None
                        break
                    time.sleep(max(0.0, PING_INTERVAL_MS/1000.0))

                # 最终确认
                final_ok = False
                final_outs: List[str] = []
                for _ in range(max(1, PING_FINAL_TRIES)):
                    ok2 = ssh.ping(ip, count=1, timeout=max(1, int(PING_FINAL_TIMEOUT_MS/1000)))
                    final_outs.append(f"ssh_ping {ip} -> {ok2}")
                    final_ok = ok2 or final_ok

                res[ip] = {"first_ok": t_first, "attempts_tail": attempts[-3:], "final": final_ok, "final_outs": final_outs[-2:]}
        finally:
            ssh.close()
    except Exception as e:
        res = {"__error__": str(e)}
    return res

def summarize_first_ok(res: Dict[str, Any]) -> Optional[float]:
    tmax: Optional[float] = None
    for v in res.values():
        if isinstance(v, dict) and v.get("first_ok") is not None:
            t = float(v["first_ok"])
            tmax = max(tmax or 0.0, t)
    return tmax

def all_final_ok(res: Dict[str, Any]) -> Optional[bool]:
    if not res:
        return None
    if "__error__" in res:
        return False
    finals = [v.get("final") for v in res.values() if isinstance(v, dict)]
    return all(bool(x) for x in finals) if finals else None


# -------------------- 电源电流门槛 --------------------
def wait_current_threshold(psu: Optional[OwonPSU],
                           threshold_ma: float,
                           hold_ms: int,
                           timeout_s: float,
                           event: callable) -> bool:
    """
    返回 True 表示在超时内达标；False 表示超时未达标（会继续流程，但日志有记录）
    """
    if psu is None:
        # 没有电源接口：不做硬判据，给个友好日志
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
        if ma >= threshold_ma:
            ok_since = ok_since or time.time()
            if (time.time() - ok_since) * 1000.0 >= hold_ms:
                event({"evt": "POWER_OK", "ma": round(ma, 1)})
                return True
        else:
            ok_since = None
        time.sleep(0.1)

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
                    psu.on()
                self.psu = psu
            except Exception as e:
                self._event({"evt": "PSU_WARN", "msg": f"电源不可用：{e}"})
                self.psu = None

    def _event(self, obj: Dict[str, Any]):
        _log_event(self.events_path, obj)

    # 主机 ping 一轮
    def _pings_host(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        for ip in HOST_PROBE_IPS:
            t, attempts = wait_first_ok_local(ip, total_timeout_s=PING_TIMEOUT_S, interval_ms=PING_INTERVAL_MS)
            final_ok, outs = final_n_local(ip, tries=PING_FINAL_TRIES, timeout_ms=PING_FINAL_TIMEOUT_MS)
            res[ip] = {"first_ok": t, "attempts_tail": attempts[-3:], "final": final_ok, "final_outs": outs[-2:]}
        return res

    # 远端 ping 一轮
    def _pings_ssh(self, host: Optional[str]) -> Dict[str, Any]:
        return wait_first_ok_ssh(host) if host else {}

    def _pick_wake_delay(self) -> Tuple[bool, float]:
        if WAKE_RANDOM_ENABLED:
            delay = random.uniform(min(WAKE_MIN_WAIT_S, WAKE_MAX_WAIT_S), max(WAKE_MIN_WAIT_S, WAKE_MAX_WAIT_S))
            return True, delay
        return False, max(0.0, WAKE_FIXED_WAIT_S)

    def _wake_start(self) -> PeriodicSender:
        payload = bytes.fromhex(WAKE_PAYLOAD_HEX)
        per = max(0.02, WAKE_PERIOD_MS/1000.0)
        task = PeriodicSender(self.bus, WAKE_ID, payload, per).start()
        self._event({"evt": "WAKE_START", "id": hex(WAKE_ID), "period_ms": WAKE_PERIOD_MS, "payload": WAKE_PAYLOAD_HEX})
        return task

    def _wake_stop(self, task: Optional[PeriodicSender]):
        if task:
            try:
                task.stop()
            except Exception:
                pass
        self._event({"evt": "WAKE_STOP"})

    def run(self):
        try:
            # ===== 首次上电 =====
            mode = "wake_on_boot" if FIRST_BOOT_SEND_WAKE else "free_run"
            boot_task: Optional[PeriodicSender] = None
            first_out_tx_seen_ts: Optional[float] = None

            if FIRST_BOOT_SEND_WAKE:
                # 上电即发 0x795
                boot_task = self._wake_start()
            else:
                # 不发 0x795，记录“首次看到 OUT_TX”的时间
                first_out_tx_seen_ts = wait_first_seen(self.bus, self.out_tx_ids, timeout_s=60.0)
                if first_out_tx_seen_ts:
                    self._event({"evt": "FIRST_OUT_TX_SEEN", "t": first_out_tx_seen_ts})

            # 等电流达标（无PSU则跳过硬判）
            wait_current_threshold(
                psu=self.psu,
                threshold_ma=POWER_WAKE_THRESHOLD_MA,
                hold_ms=POWER_HOLD_MS,
                timeout_s=POWER_TIMEOUT_S,
                event=self._event
            )

            # 做三路 ping
            host_res = self._pings_host()
            main_res = self._pings_ssh(SSH_MAIN)
            backup_res = self._pings_ssh(SSH_BACKUP)

            host_first_ok = summarize_first_ok(host_res)
            main_first_ok = summarize_first_ok(main_res) if main_res else None
            backup_first_ok = summarize_first_ok(backup_res) if backup_res else None

            # 停止 0x795 或者确定计时基准
            if FIRST_BOOT_SEND_WAKE:
                if FIRST_BOOT_STOP_WAKE_AFTER_PINGS:
                    self._wake_stop(boot_task)
                    stop_time = time.time()
                else:
                    # 如不希望此刻停，可在后续任何时机再停；这里为了休眠判定仍需要停发
                    self._wake_stop(boot_task)
                    stop_time = time.time()
            else:
                # 不发 0x795 时，把“首次看到 OUT_TX”的时间当作计时基准
                stop_time = first_out_tx_seen_ts or time.time()

            # 等待休眠（监听 OUT_TX 静默）
            sd_res = wait_until_asleep(
                bus=self.bus,
                out_tx_ids=self.out_tx_ids,
                silence_window_s=SLEEP_SILENCE_S,
                max_wait_s=SLEEP_MAX_WAIT_S,
                stop_time=stop_time,
                event_log=self._event,
                pre_drain_s=SLEEP_PREDRAIN_S
            )

            # 写入首次上电的摘要
            _append_summary(self.summary_path, [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1, mode, None, None,
                None if host_first_ok is None else round(host_first_ok, 3),
                None if main_first_ok is None else round(main_first_ok, 3),
                None if backup_first_ok is None else round(backup_first_ok, 3),
                all_final_ok(host_res),
                ("__error__" not in main_res) if main_res else None,
                ("__error__" not in backup_res) if backup_res else None,
                None if sd_res.get("elapsed_from_stop_s") is None else round(sd_res.get("elapsed_from_stop_s"), 3),
                sd_res.get("status"),
                None if sd_res.get("total_wait_s") is None else round(sd_res.get("total_wait_s"), 3),
                None if first_out_tx_seen_ts is None else round(first_out_tx_seen_ts, 3),
                json.dumps({"seen_counter": sd_res.get("seen_counter", {})}, ensure_ascii=False)
            ])

            print(
                f"[#1] host_first_ok={None if host_first_ok is None else round(host_first_ok,3)}s | "
                f"sleep_status={sd_res.get('status')} | stop→last_seen={sd_res.get('elapsed_from_stop_s')}s"
            )

            # ===== 后续循环 =====
            for cycle in range(2, CYCLES + 1):
                used_random, delay_s = self._pick_wake_delay()
                # 休眠后等待（固定/随机）
                time.sleep(delay_s)

                # 唤醒：开始发 0x795
                task = self._wake_start()

                # 等电流达标
                wait_current_threshold(
                    psu=self.psu,
                    threshold_ma=POWER_WAKE_THRESHOLD_MA,
                    hold_ms=POWER_HOLD_MS,
                    timeout_s=POWER_TIMEOUT_S,
                    event=self._event
                )

                # 做三路 ping
                host_res = self._pings_host()
                main_res = self._pings_ssh(SSH_MAIN)
                backup_res = self._pings_ssh(SSH_BACKUP)
                host_first_ok = summarize_first_ok(host_res)
                main_first_ok = summarize_first_ok(main_res) if main_res else None
                backup_first_ok = summarize_first_ok(backup_res) if backup_res else None

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

                # 摘要
                _append_summary(self.summary_path, [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cycle,
                    "loop", used_random, round(delay_s, 1),
                    None if host_first_ok is None else round(host_first_ok, 3),
                    None if main_first_ok is None else round(main_first_ok, 3),
                    None if backup_first_ok is None else round(backup_first_ok, 3),
                    all_final_ok(host_res),
                    ("__error__" not in main_res) if main_res else None,
                    ("__error__" not in backup_res) if backup_res else None,
                    None if sd_res.get("elapsed_from_stop_s") is None else round(sd_res.get("elapsed_from_stop_s"), 3),
                    sd_res.get("status"),
                    None if sd_res.get("total_wait_s") is None else round(sd_res.get("total_wait_s"), 3),
                    None,
                    json.dumps({"seen_counter": sd_res.get("seen_counter", {})}, ensure_ascii=False)
                ])

                print(
                    f"[#{cycle}] wait={round(delay_s,1)}s({ 'rand' if used_random else 'fixed' }) | "
                    f"host_first_ok={None if host_first_ok is None else round(host_first_ok,3)}s | "
                    f"sleep_status={sd_res.get('status')} | stop→last_seen={sd_res.get('elapsed_from_stop_s')}s"
                )

        finally:
            # 资源释放
            try:
                self.canx.close()
            except Exception:
                pass
            if self.psu:
                try:
                    if PSU_SET_OUTPUT:
                        self.psu.off()
                except Exception:
                    pass


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    Runner().run()

if __name__ == "__main__":
    main()
