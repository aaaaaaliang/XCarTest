#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBOX 休眠/唤醒循环验证（纯 CAN + Ping）—— 默认跑 100 轮，无需命令行参数

流程：
1) 开始一轮时，周期外发 0x795 唤醒；
   - 在外发期间做三路 Ping：
     a) 本机→外部 IP 列表（EXT_IPS）
     b) SSH 登录主模组，在模组内部对目标 IP（默认同 EXT_IPS）Ping
     c) SSH 登录副模组，在模组内部对目标 IP Ping
   - 三路 Ping（成功或超时）都完成后，**立即停止外发 0x795**。
2) 停止外发 0x795 后，监听指定的“周期性外发报文 ID”（OUT_TX_IDS），直到它们彻底静默：
   - 以最后一次看到这些 ID 的时间为“CAN 停止外发时间（last_seen）”；
   - 从停止外发（stop_795）到 last_seen 的时差 = 你要的“**停止外发→彻底休眠历时**”；
   - 额外设一个“静默窗口 SLEEP_SILENCE_S”（通常取 3×最大周期），若超出该窗口仍没再看到 OUT_TX_IDS，即判定“已休眠”。
3) 进入休眠后，进入下一轮：再发 0x795 唤醒→重复上述（共 CYCLES 次）。

日志：
- logs/sleep_wake_events_YYYYMMDD.log    事件 JSON 行（WAKE_START/STOP、看到的 OUT_TX、Ping 尝试尾部等）
- logs/sleep_wake_summary_YYYYMMDD.csv   每轮摘要（各路 Ping 首次打通、最终是否 OK、stop→last_seen、休眠状态等）

★ 本脚本 **无需命令行参数**，所有配置在下方“默认配置区”集中填写；
  同时也支持用环境变量覆盖（可选），比如 SSH_USER/SSH_PASSWORD/SSH_PKEY/SSH_PORT。

依赖：
  pip install python-can paramiko

注意：
- 若未安装 python-can/paramiko，会自动进入“跳过”模式并给出友好提示。
- Vector 场景需安装 Vector 驱动；接口选择通过 CAN_INTERFACE/CAN_CHANNEL/CAN_APP_NAME。
"""

from __future__ import annotations
import os
import json
import csv
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set

# ========================= 默认配置区（按现场改这里） =========================
# 三路 Ping 目标
EXT_IPS: List[str] = ["192.168.1.99", "192.168.1.9", "192.168.1.1"]
SSH_MAIN: Optional[str] = "192.168.1.201"   # 主模组 IP；无则设为 None
SSH_BACKUP: Optional[str] = "192.168.1.202" # 副模组 IP；无则设为 None
SSH_USER: Optional[str] = os.getenv("SSH_USER", "root")
SSH_PASS: Optional[str] = os.getenv("SSH_PASSWORD", "123456") if os.getenv("SSH_PKEY") is None else None
SSH_KEY:  Optional[str] = os.getenv("SSH_PKEY", None)  # 私钥路径（与密码二选一）
SSH_PORT: int = int(os.getenv("SSH_PORT", "22"))

# CAN 接口（python-can v4.2+ 用 interface 写法）
CAN_INTERFACE: str = "vector"     # vector / pcan / kvaser / socketcan ...
CAN_CHANNEL: int | str = 0         # Vector: 0/1/...；socketcan: "can0"
CAN_APP_NAME: Optional[str] = "CANoe"  # 仅 Vector 需要
CAN_BITRATE: Optional[int] = None  # 非 Vector 接口才需要设定，例如 500000

# 唤醒报文 0x795
WAKE_ID: int = 0x795
WAKE_PAYLOAD_HEX: str = "0000000000000000"
WAKE_PERIOD_MS: int = 100

# 用于判定“休眠”的 TBOX 周期外发 ID（勾通库里的 4 个信号）
OUT_TX_IDS: List[int | str] = ["0x5A0", "0x603", "0x6C0", "0x6C1"]

# 休眠判据
SLEEP_SILENCE_S: float = 3.0   # 静默窗口（建议≥3×最大周期）
SLEEP_MAX_WAIT_S: float = 120.0
SLEEP_PREDRAIN_S: float = 0.2  # 统计前预排空接收队列，避免把历史帧算进来

# Ping 参数
PING_TIMEOUT_S: float = 10.0
PING_INTERVAL_MS: int = 800
PING_FINAL_TRIES: int = 3
PING_FINAL_TIMEOUT_MS: int = 1200

# 循环次数
CYCLES: int = 100

# 日志目录
LOG_DIR: str = "logs"
# =============================================================================

# --------- 可选依赖：python-can ---------
try:
    import can  # type: ignore
    HAS_CAN = True
except Exception:
    HAS_CAN = False

# --------- 可选依赖：paramiko（SSH） ---------
try:
    import paramiko  # type: ignore
    HAS_PARAMIKO = True
except Exception:
    HAS_PARAMIKO = False

# --------- 本机 ping ---------
import platform
import subprocess
IS_WINDOWS = platform.system().lower().startswith("win")

def _run_local_ping_once(ip: str, timeout_ms: int = 1000) -> Tuple[bool, str]:
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_ms)), ip]
    else:
        sec = max(1, int(round(timeout_ms/1000)))
        cmd = ["sh", "-lc", f"ping -c 1 -W {sec} {ip} || ping -c 1 -w {sec} {ip}"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=max(1, int(timeout_ms/1000)+1))
        ok = (p.returncode == 0)
        out = p.stdout.strip().replace("\n", " | ")[-500:]
        return ok, out
    except Exception as e:
        return False, f"EXC:{e}"

def _wait_first_ok(ip: str, total_timeout_s: float, interval_ms: int) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    start = time.time(); attempts: List[Dict[str, Any]] = []
    while True:
        ok, out = _run_local_ping_once(ip, timeout_ms=interval_ms)
        attempts.append({"t": round(time.time()-start, 3), "ok": ok, "out": out})
        if ok:
            return time.time()-start, attempts
        if time.time() - start >= total_timeout_s:
            return None, attempts
        time.sleep(max(0.0, interval_ms/1000.0))

def _final_n_attempts(ip: str, tries: int, timeout_ms: int) -> Tuple[bool, List[str]]:
    outs: List[str] = []
    ok = False
    for _ in range(max(1, tries)):
        ok, out = _run_local_ping_once(ip, timeout_ms=timeout_ms)
        outs.append(out)
    return ok, outs

# --------- SSH 内部 ping ---------
class SSHProbe:
    def __init__(self, host: str, port: int, user: Optional[str], password: Optional[str], pkey_path: Optional[str], timeout: int = 15):
        if not HAS_PARAMIKO:
            raise RuntimeError("未安装 paramiko，无法进行 SSH 探测")
        self.host=host; self.port=port; self.user=user; self.password=password; self.pkey_path=pkey_path; self.timeout=timeout
        self.client: Optional[paramiko.SSHClient] = None
    def __enter__(self):
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.pkey_path:
            key = paramiko.RSAKey.from_private_key_file(self.pkey_path)
            c.connect(self.host, port=self.port, username=self.user, pkey=key, timeout=self.timeout)
        else:
            c.connect(self.host, port=self.port, username=self.user, password=self.password, timeout=self.timeout)
        self.client = c; return self
    def __exit__(self, et, ev, tb):
        try:
            if self.client: self.client.close()
        except Exception:
            pass
    def ping_once(self, ip: str, timeout_ms: int = 1000) -> Tuple[bool, str]:
        assert self.client is not None
        sec = max(1, int(round(timeout_ms/1000)))
        cmd = f"ping -c 1 -W {sec} {ip} || ping -c 1 -w {sec} {ip}"
        _, stdout, _ = self.client.exec_command(cmd, timeout=self.timeout)
        rc = stdout.channel.recv_exit_status(); out = stdout.read().decode("utf-8","ignore").strip().replace("\n"," | ")[-500:]
        return (rc==0), out
    def wait_first_ok(self, ip: str, total_timeout_s: float, interval_ms: int) -> Tuple[Optional[float], List[Dict[str, Any]]]:
        start = time.time(); attempts: List[Dict[str, Any]] = []
        while True:
            ok, out = self.ping_once(ip, timeout_ms=interval_ms)
            attempts.append({"t": round(time.time()-start, 3), "ok": ok, "out": out})
            if ok: return time.time()-start, attempts
            if time.time()-start >= total_timeout_s: return None, attempts
            time.sleep(max(0.0, interval_ms/1000.0))
    def final_n_attempts(self, ip: str, tries: int, timeout_ms: int) -> Tuple[bool, List[str]]:
        outs: List[str] = []; ok = False
        for _ in range(max(1, tries)):
            ok, out = self.ping_once(ip, timeout_ms=timeout_ms); outs.append(out)
        return ok, outs

# --------- CAN 适配 ---------
class ThreadPeriodic:
    def __init__(self, bus, arb_id: int, data: bytes, period_s: float):
        import threading
        self._bus = bus; self._id = arb_id; self._data = data; self._per = max(0.01, period_s)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
    def _run(self):
        import threading
        msg = can.Message(arbitration_id=self._id, data=self._data, is_extended_id=False)
        while not self._stop.is_set():
            try:
                self._bus.send(msg)
            except Exception:
                pass
            self._stop.wait(self._per)
    def stop(self):
        self._stop.set()
        try:
            self._th.join(timeout=1.0)
        except Exception:
            pass

class CANAdapter:
    def __init__(self, interface: str, channel: int | str, app_name: Optional[str], bitrate: Optional[int]):
        self.bus = None
        if not HAS_CAN:
            print("[WARN] 未安装 python-can，进入‘无 CAN’占位模式（不会真正发/收帧）")
            return
        kw: Dict[str, Any] = {"interface": interface}
        if interface.lower() == "vector":
            kw["channel"] = int(channel) if str(channel).isdigit() else channel
            if app_name:
                kw["app_name"] = app_name
        else:
            kw["channel"] = channel
            if bitrate:
                kw["bitrate"] = bitrate
        self.bus = can.Bus(**kw)
    def send_periodic(self, arb_id: int, data: bytes, period_s: float):
        if not HAS_CAN or self.bus is None:
            return ThreadPeriodicDummy(arb_id, data, period_s)
        try:
            msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
            task = self.bus.send_periodic(msg, period=period_s)
            return task
        except Exception:
            # 某些接口未实现 send_periodic，用线程兜底
            return ThreadPeriodic(self.bus, arb_id, data, period_s)
    def recv(self, timeout: float = 0.1):
        if not HAS_CAN or self.bus is None:
            time.sleep(timeout); return None
        try:
            return self.bus.recv(timeout)
        except Exception:
            return None
    def pre_drain(self, seconds: float):
        if not HAS_CAN or self.bus is None:
            time.sleep(seconds); return
        end = time.time() + seconds
        while time.time() < end:
            try:
                while self.bus.recv(timeout=0.0) is not None:
                    pass
            except Exception:
                break
            time.sleep(0.01)

class ThreadPeriodicDummy:
    def __init__(self, arb_id: int, data: bytes, period_s: float):
        self.arb_id=arb_id; self.data=data; self.period_s=period_s
    def stop(self):
        pass

# --------- 休眠判据（监听 OUT_TX_IDS 静默） ---------
class SleepDetector:
    def __init__(self, canx: CANAdapter, out_tx_ids: Set[int], silence_window_s: float, max_wait_s: float, pre_drain_s: float):
        self.canx = canx
        self.out_tx_ids = out_tx_ids
        self.silence_window_s = silence_window_s
        self.max_wait_s = max_wait_s
        self.pre_drain_s = pre_drain_s
    def wait_until_asleep(self, stop_time: float, event_log: callable) -> Dict[str, Any]:
        self.canx.pre_drain(self.pre_drain_s)
        last_seen: Optional[float] = None
        seen_counter: Dict[int, int] = {}
        start = time.time()
        while True:
            now = time.time()
            if now - start > self.max_wait_s:
                return {
                    "status": "TIMEOUT",
                    "last_seen": last_seen,
                    "silence_window_s": self.silence_window_s,
                    "seen_counter": {hex(k): v for k,v in seen_counter.items()},
                    "elapsed_from_stop_s": now - stop_time
                }
            frame = self.canx.recv(timeout=0.05)
            if frame is None:
                # 判定静默（从最后一次看到 OUT_TX 起，超过静默窗口）
                if last_seen is not None and (now - last_seen) >= self.silence_window_s and now >= stop_time:
                    return {
                        "status": "ASLEEP",
                        "last_seen": last_seen,
                        "silence_window_s": self.silence_window_s,
                        "seen_counter": {hex(k): v for k,v in seen_counter.items()},
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
            if arb in self.out_tx_ids:
                ts = time.time(); last_seen = ts
                seen_counter[arb] = seen_counter.get(arb, 0) + 1
                event_log({"evt":"OUT_TX","id": hex(arb), "t": round(ts - stop_time, 3)})

# --------- 业务流程 ---------
class Runner:
    def __init__(self):
        # 处理 OUT_TX_IDS（支持字符串 0x.. 或整数）
        out_ids: Set[int] = set()
        for x in OUT_TX_IDS:
            if isinstance(x, str):
                x = x.strip()
                out_ids.add(int(x, 16) if x.lower().startswith("0x") else int(x))
            else:
                out_ids.add(int(x))
        self.out_tx_ids = out_ids

        # 路径
        os.makedirs(LOG_DIR, exist_ok=True)
        self.events_path = os.path.join(LOG_DIR, f"sleep_wake_events_{datetime.now().strftime('%Y%m%d')}.log")
        self.summary_path = os.path.join(LOG_DIR, f"sleep_wake_summary_{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(self.summary_path):
            with open(self.summary_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts","cycle","ext_first_ok_s","main_first_ok_s","backup_first_ok_s","ext_all_final_ok","main_ok","backup_ok","stop_to_last_seen_s","sleep_status","sleep_total_wait_s","notes"])

        # CAN 适配
        self.canx = CANAdapter(CAN_INTERFACE, CAN_CHANNEL, CAN_APP_NAME, CAN_BITRATE)

    def log_event(self, obj: Dict[str, Any]):
        obj["ts"] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # 三路 Ping
    def _pings_external(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        for ip in EXT_IPS:
            t, attempts = _wait_first_ok(ip, total_timeout_s=PING_TIMEOUT_S, interval_ms=PING_INTERVAL_MS)
            final_ok, outs = _final_n_attempts(ip, tries=PING_FINAL_TRIES, timeout_ms=PING_FINAL_TIMEOUT_MS)
            res[ip] = {"first_ok": t, "attempts_tail": attempts[-3:], "final": final_ok, "final_outs": outs[-2:]}
        return res
    def _pings_ssh(self, host: Optional[str]) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        if host and HAS_PARAMIKO:
            try:
                with SSHProbe(host, SSH_PORT, SSH_USER, SSH_PASS, SSH_KEY) as sp:
                    targets = EXT_IPS
                    for ip in targets:
                        t, attempts = sp.wait_first_ok(ip, total_timeout_s=PING_TIMEOUT_S, interval_ms=PING_INTERVAL_MS)
                        final_ok, outs = sp.final_n_attempts(ip, tries=PING_FINAL_TRIES, timeout_ms=PING_FINAL_TIMEOUT_MS)
                        res[ip] = {"first_ok": t, "attempts_tail": attempts[-3:], "final": final_ok, "final_outs": outs[-2:]}
            except Exception as e:
                res = {"__error__": str(e)}
        elif host and not HAS_PARAMIKO:
            res = {"__error__": "paramiko 未安装"}
        return res

    @staticmethod
    def _summarize_first_ok(res: Dict[str, Any]) -> Optional[float]:
        tmax: Optional[float] = None
        for v in res.values():
            if isinstance(v, dict) and v.get("first_ok") is not None:
                t = float(v["first_ok"])  # type: ignore
                tmax = max(tmax or 0.0, t)
        return tmax

    @staticmethod
    def _all_final_ok(res: Dict[str, Any]) -> Optional[bool]:
        if not res:
            return None
        if "__error__" in res:
            return False
        finals = [v.get("final") for v in res.values() if isinstance(v, dict)]
        return all(bool(x) for x in finals) if finals else None

    def run_one_cycle(self, cycle: int):
        # A) 发 0x795 唤醒（周期）
        payload = bytes.fromhex(WAKE_PAYLOAD_HEX)
        per = max(0.02, WAKE_PERIOD_MS/1000.0)
        task = self.canx.send_periodic(WAKE_ID, payload, per)
        self.log_event({"cycle": cycle, "evt": "WAKE_START", "id": hex(WAKE_ID), "period_ms": WAKE_PERIOD_MS, "payload": WAKE_PAYLOAD_HEX})

        # B) 三路 Ping（在发 0x795 期间进行）
        ext_res = self._pings_external()
        main_res = self._pings_ssh(SSH_MAIN) if SSH_MAIN else {}
        backup_res = self._pings_ssh(SSH_BACKUP) if SSH_BACKUP else {}
        ext_first_ok = self._summarize_first_ok(ext_res)
        main_first_ok = self._summarize_first_ok(main_res) if main_res else None
        backup_first_ok = self._summarize_first_ok(backup_res) if backup_res else None

        # C) 停止 0x795 外发，并记录 stop_time
        try:
            task.stop()
        except Exception:
            pass
        stop_time = time.time()
        self.log_event({"cycle": cycle, "evt": "WAKE_STOP"})

        # D) 等待休眠（监听 OUT_TX_IDS 静默）
        sd = SleepDetector(self.canx, self.out_tx_ids, SLEEP_SILENCE_S, SLEEP_MAX_WAIT_S, SLEEP_PREDRAIN_S)
        sd_res = sd.wait_until_asleep(stop_time, self.log_event)

        # E) 写 CSV 摘要
        with open(self.summary_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cycle,
                None if ext_first_ok is None else round(ext_first_ok,3),
                None if main_first_ok is None else round(main_first_ok,3),
                None if backup_first_ok is None else round(backup_first_ok,3),
                self._all_final_ok(ext_res),
                ("__error__" not in main_res) if main_res else None,
                ("__error__" not in backup_res) if backup_res else None,
                None if sd_res.get("elapsed_from_stop_s") is None else round(sd_res.get("elapsed_from_stop_s"),3),
                sd_res.get("status"),
                None if sd_res.get("total_wait_s") is None else round(sd_res.get("total_wait_s"),3),
                json.dumps({"seen_counter": sd_res.get("seen_counter", {})}, ensure_ascii=False)
            ])

        # F) 控制台摘要（给人工快速看）
        print(
            f"[#{cycle}] ext_first_ok={None if ext_first_ok is None else round(ext_first_ok,3)}s | "
            f"sleep_status={sd_res.get('status')} | stop→last_seen={sd_res.get('elapsed_from_stop_s')}s"
        )

    def run(self):
        for i in range(1, CYCLES + 1):
            self.run_one_cycle(i)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    Runner().run()


if __name__ == "__main__":
    main()
