# -*- coding: utf-8 -*-
"""
电源 x CAN 联动验证脚本（硬编码，独立可跑，支持重复次数 & 按轮统计）
目标：在不同电压（5.8/7.2/12.0/18.2/18.6V）下，验证 TBOX 规定外发报文是否“该发/该停”。

依赖：
  pip install pyserial python-can

用法示例：
  python a.py                      # 跑1轮
  python a.py --repeat 5           # 跑5轮
  python a.py --sample 8           # 每点抓包8s
  python a.py --extra_off_wait 3   # “应停发”点位额外等待3s再抓
  python a.py --min_on_ids 2       # “应外发”判定阈值：至少看到N个目标ID算通过
  python a.py --keep_awake 1       # 是否周期发送0x795保持唤醒(1=是,0=否)
"""

import os, time, csv, datetime, argparse
import serial
import can
import threading

# =============== 配置区（按现场修改） ===============
REPEAT_TIMES = 100

# 电源（OWON SP6101）串口
PSU_COM = "COM3"
PSU_BAUD = 115200
PSU_TIMEOUT = 1.0

# CAN（python-can）
CAN_INTERFACE = "vector"   # 旧写法 bustype 已弃用；这里用 interface
CAN_CHANNEL   = 0          # Vector: CAN1=0, CAN2=1...
CAN_BITRATE   = 500000
CAN_APP_NAME  = "CANoe"    # 非 Vector 可删

# 待验证的外发 ID（仅 tcan）
EXPECTED_IDS = {0x5A0, 0x6C0, 0x6C1, 0x603}

# 规范点位与期望（True=应外发，False=应停发）
V_CASES = [
    (5.5,  False),  # 欠压 ≤6V → 停发
    (12.0, True ),  # 正常 → 外发
    (18.5, False),  # 过压 ≥18V → 停发
]

# 不同电压使用的限流（防止高压大电流冲击）
def current_limit_for(v: float) -> float:
    return 0.5 if v >= 18.0 else 2.0

# 电压稳态判定：目标 ±0.1V 连续 ≥600ms（规范 ≥500ms）
STABLE_TOL_V     = 0.1
STABLE_HOLD_MS   = 600
STABLE_TIMEOUT_S = 10

# 进入“应停发”点位后，额外等待（给ECU留收敛时间）
EXTRA_WAIT_OFF_S = 2.0

# CAN 抓包时长（若外发周期长，改到 5~10s）
CAN_SAMPLE_S = 3.0

# “应外发”判定阈值：看到的目标ID个数 ≥ MIN_ON_IDS 即通过（默认 2）
MIN_ON_IDS = 2

# 是否周期发送 0x795 保持唤醒（1=是，0=否）
KEEP_AWAKE_DEFAULT = 1

# 结果 CSV
TS = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
RESULT_CSV  = f"power_can_result_{TS}.csv"
SUMMARY_CSV = f"power_can_result_summary_{TS}.csv"
# ======================================================


# ========= 电源封装 =========
class OwonPSU:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def cmd(self, c: str, wait: float = 0.2) -> str:
        self.ser.write((c + "\r\n").encode())
        time.sleep(wait)
        resp = self.ser.read_all().decode(errors="ignore").strip()
        print(f">>> {c}\n<<< {resp}")
        return resp

    def init_remote(self):
        self.cmd("*IDN?")
        self.cmd("*RST")
        self.cmd("SYSTEM:LOCK ON")
        self.cmd("OUTP OFF")

    def set_vi(self, v: float, a: float):
        self.cmd(f"VOLT {v}")
        self.cmd(f"CURR {a}")

    def out_on(self):
        self.cmd("OUTP ON")
        time.sleep(0.2)

    def out_off(self):
        self.cmd("OUTP OFF")

    def meas_v(self) -> float:
        r = self.cmd("MEAS:VOLT?")
        try:
            return float(r)
        except:
            return 0.0

    def meas_a(self) -> float:
        r = self.cmd("MEAS:CURR?")
        try:
            return float(r)
        except:
            return 0.0

    def wait_voltage_stable(self, target: float, tol: float = STABLE_TOL_V,
                            hold_ms: int = STABLE_HOLD_MS, timeout_s: int = STABLE_TIMEOUT_S):
        end = time.time() + timeout_s
        ok_since = None
        while time.time() < end:
            v = self.meas_v()
            if abs(v - target) <= tol:
                ok_since = ok_since or time.time()
                if (time.time() - ok_since) * 1000.0 >= hold_ms:
                    return
            else:
                ok_since = None
            time.sleep(0.05)
        raise AssertionError(f"电压未能在{timeout_s}s内稳定到 {target:.2f}±{tol}V 并保持 {hold_ms}ms")


# ========= CAN 封装 =========
def open_can_bus():
    kwargs = dict(interface=CAN_INTERFACE, channel=CAN_CHANNEL, bitrate=CAN_BITRATE)
    if CAN_INTERFACE == "vector" and CAN_APP_NAME:
        kwargs["app_name"] = CAN_APP_NAME
    return can.Bus(**kwargs)

def collect_can_ids(bus: can.BusABC, sample_s: float, ids_focus: set[int], pre_drain_s: float = 0.2) -> set[int]:
    """
    纯软件过滤：抓取 sample_s 秒，再在内存里按 ids_focus 过滤（兼容Vector的硬件滤波限制）。
    加了 pre_drain_s 预排空，避免把启动/历史帧算进来。
    """
    reader = can.BufferedReader()
    notifier = can.Notifier(bus, [reader], timeout=0.1)

    # 先“排空”硬件队列/瞬态启动帧
    t0 = time.time()
    while time.time() - t0 < pre_drain_s:
        while reader.get_message() is not None:
            pass
        time.sleep(0.01)

    # 正式采集
    time.sleep(sample_s)
    notifier.stop()

    seen = set()
    while True:
        msg = reader.get_message()
        if msg is None:
            break
        try:
            if not msg.is_extended_id and msg.arbitration_id in ids_focus:
                seen.add(msg.arbitration_id)
        except Exception:
            pass
    return seen

def keep_awake_task(bus: can.BusABC, stop_event: threading.Event):
    """后台循环发送 0x795 保持唤醒"""
    msg = can.Message(arbitration_id=0x795, data=[0x00]*8, is_extended_id=False)
    while not stop_event.is_set():
        try:
            bus.send(msg)
            # print("[KeepAwake] Sent 0x795")  # 可选调试
        except Exception as e:
            print("[KeepAwake] 发送失败:", e)
        time.sleep(1.0)  # 1s 一次


# ========= 主流程 =========
def parse_args():
    ap = argparse.ArgumentParser(description="电源xCAN 外发验证（支持重复次数 & 按轮统计）")
    ap.add_argument("--repeat", type=int, default=REPEAT_TIMES, help="重复执行次数（轮数）")
    ap.add_argument("--sample", type=float, default=CAN_SAMPLE_S, help="每点抓包时长(秒)")
    ap.add_argument("--extra_off_wait", type=float, default=EXTRA_WAIT_OFF_S, help="应停发点位额外等待(秒)")
    ap.add_argument("--min_on_ids", type=int, default=MIN_ON_IDS, help="应外发判定阈值：至少抓到N个目标ID算通过")
    ap.add_argument("--keep_awake", type=int, default=KEEP_AWAKE_DEFAULT, help="是否周期发送0x795保持唤醒(1=是,0=否)")
    return ap.parse_args()

def main():
    args = parse_args()
    repeat = max(1, int(args.repeat))
    sample = float(args.sample)
    extra_off_wait = float(args.extra_off_wait)
    min_on_ids = max(1, int(args.min_on_ids))
    keep_awake = int(args.keep_awake) == 1

    print("=== 初始化电源 ===")
    psu = OwonPSU(PSU_COM, PSU_BAUD, PSU_TIMEOUT)
    psu.init_remote()

    print("=== 打开 CAN 通道 ===")
    bus = open_can_bus()

    # 启动保持唤醒线程（可开关）
    stop_event = threading.Event()
    thread = None
    if keep_awake:
        thread = threading.Thread(target=keep_awake_task, args=(bus, stop_event), daemon=True)
        thread.start()

    # 点位结果明细 CSV
    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f_detail, \
         open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as f_sum:

        wr_detail = csv.writer(f_detail)
        wr_detail.writerow([
            "time", "run_idx", "point_idx",
            "voltage_set(V)", "current_set(A)",
            "meas_v(V)", "meas_a(A)", "expect_on",
            "seen_ids", "seen_cnt", "min_on_ids", "missing_ids", "unexpected_ids", "pass?"
        ])

        wr_sum = csv.writer(f_sum)
        wr_sum.writerow([
            "time", "total_runs", "run_idx", "points_total", "points_passed", "run_pass?"
        ])

        total_runs_pass = 0

        for run_idx in range(1, repeat + 1):
            print(f"\n================= 第 {run_idx}/{repeat} 轮 =================")
            run_all_ok = True
            points_passed = 0

            for point_idx, (v_target, expect_on) in enumerate(V_CASES, start=1):
                i_limit = current_limit_for(v_target)
                print(f"\n=== 轮{run_idx} 点{point_idx}：{v_target:.1f} V，限流 {i_limit:.2f} A，期望外发={expect_on} ===")

                # 1) 设置电压/电流并上电
                psu.set_vi(v_target, i_limit)
                psu.out_on()

                # 2) 等电压稳定
                try:
                    psu.wait_voltage_stable(v_target)
                    print("[电源] 电压稳态达标")
                except AssertionError as e:
                    print("[电源] 稳态失败：", e)

                # “应停发”的场景，额外等待（自适应稍长一些更稳）
                if not expect_on:
                    off_wait = max(extra_off_wait, 5.0 if v_target <= 6.0 else (6.0 if v_target >= 18.0 else extra_off_wait))
                    time.sleep(off_wait)

                # 3) 记录一次测量
                mv = psu.meas_v()
                ma = psu.meas_a()

                # 4) 抓 CAN 报文（软件过滤）
                seen = collect_can_ids(bus, sample, EXPECTED_IDS)
                seen_cnt = len(seen)

                # 5) 判定
                missing    = sorted(EXPECTED_IDS - seen)
                unexpected = sorted(seen - EXPECTED_IDS)  # 理论应为空（因为已过滤）
                if expect_on:
                    passed = (seen_cnt >= min_on_ids)
                else:
                    passed = (seen_cnt == 0)

                print(f"[CAN] 抓到ID：{[hex(x) for x in sorted(seen)]} (数量={seen_cnt}, 阈值={min_on_ids if expect_on else 0})")
                if expect_on and not passed:
                    print(f"[判定] 失败：应外发但抓到数量({seen_cnt}) < 阈值({min_on_ids})；缺少 -> {[hex(x) for x in missing]}")
                if not expect_on and not passed:
                    print(f"[判定] 失败：应停发但抓到 -> {[hex(x) for x in sorted(seen)]}")
                if expect_on and passed:
                    print("[判定] 通过：应发场景抓到的目标ID数量达到阈值")
                if not expect_on and passed:
                    print("[判定] 通过：应停发场景未见目标ID")

                # 点位写明细
                wr_detail.writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    run_idx, point_idx,
                    f"{v_target:.3f}", f"{i_limit:.3f}",
                    f"{mv:.3f}", f"{ma:.3f}", expect_on,
                    " ".join(hex(x) for x in sorted(seen)),
                    seen_cnt, min_on_ids,
                    " ".join(hex(x) for x in sorted(missing)),
                    " ".join(hex(x) for x in sorted(unexpected)),
                    "PASS" if passed else "FAIL"
                ])

                # 轮内累计
                if passed:
                    points_passed += 1
                run_all_ok &= passed

                # 7) 每个点位测完可关输出
                psu.out_off()
                time.sleep(0.3)

            # 本轮汇总
            if run_all_ok:
                total_runs_pass += 1
            wr_sum.writerow([
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                repeat, run_idx, len(V_CASES), points_passed,
                "PASS" if run_all_ok else "FAIL"
            ])

            print(f"\n—— 第 {run_idx} 轮结果：{points_passed}/{len(V_CASES)} 点通过，"
                  f"该轮判定：{'PASS' if run_all_ok else 'FAIL'} ——")

    # 资源释放（先停唤醒线程，再关总线/电源）
    stop_event.set()
    if thread is not None:
        try:
            thread.join(timeout=1.5)
        except Exception:
            pass

    try:
        bus.shutdown()
    except Exception:
        pass
    psu.close()

    # 总结
    pass_rate = 100.0 * total_runs_pass / repeat
    print(f"\n=== 全部完成 ===")
    print(f"轮次通过：{total_runs_pass}/{repeat}（通过率 {pass_rate:.1f}%）")
    print(f"点位明细：{RESULT_CSV}")
    print(f"轮次汇总：{SUMMARY_CSV}")


if __name__ == "__main__":
    main()
