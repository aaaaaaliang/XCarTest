import ctypes
import ctypes.wintypes as wt
import time
import random
import csv
import os

# ================== 配置 ==================
region = (2000, 1049, 200, 154)   # 点击区域 (x, y, w, h)
run_duration = 5 * 60 * 60        # 总运行时长（秒）→ 5 小时
countdown = 5                     # 启动前倒计时（秒）

# 节奏配置
normal_rest_range = (12, 19)      # 每轮结束后的普通休息区间（秒）
rare_rest_prob = 0.10             # 触发“偶尔大休息”的概率
rare_rest_range = (30, 60)        # 偶尔大休息时长（秒）
reset_interval_range = (20*60, 40*60)  # 节奏重置间隔（秒）：每隔 20~40 分钟强制一次超长休息
reset_rest_range = (2*60, 5*60)   # 节奏重置时休息 2~5 分钟

# 连续点击细节
intra_click_sleep_range = (0.2, 0.6)  # 同一轮内相邻两次点击的间隔
micro_offset_px = 3                   # 同一轮内落点微偏移幅度（±像素）

# 批次点击次数分布（更自然：2次最多，少量 3/4 次，极少 5 次）
def sample_batch():
    r = random.random()
    if r < 0.60:
        return 2
    elif r < 0.90:
        return 3
    elif r < 0.98:
        return 4
    else:
        return 5

# 前台窗口保护（可选）：仅当窗口标题包含这些子串之一才点击；为空列表则不限制
allowed_window_title_substrings = []  # 例如 ["Chrome", "MyApp"]，留空表示不限制

# 日志（可选）
LOG_TO_FILE = False
LOG_PATH = "click_log.csv"
# =========================================


# =============== WinAPI 常量/结构 ===============
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MOUSEEVENTF_MOVE       = 0x0001
MOUSEEVENTF_LEFTDOWN   = 0x0002
MOUSEEVENTF_LEFTUP     = 0x0004
MOUSEEVENTF_ABSOLUTE   = 0x8000
MOUSEEVENTF_VIRTUALDESK= 0x4000

VK_ESCAPE = 0x1B
VK_F6     = 0x75
VK_F8     = 0x77

class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong),
                ("mi", MOUSEINPUT)]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long),
                ("y", ctypes.c_long)]

SendInput = user32.SendInput

# =============== 工具函数 ===============
def get_virtual_screen_metrics():
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return vx, vy, vw, vh

VX, VY, VW, VH = get_virtual_screen_metrics()

def to_abs(x, y):
    # 映射到 0..65535 的虚拟桌面绝对坐标（多显示器/高DPI 友好）
    ax = int((x - VX) * 65535 / max(1, VW - 1))
    ay = int((y - VY) * 65535 / max(1, VH - 1))
    return ax, ay

def send_mouse_abs(x, y, flags):
    ax, ay = to_abs(x, y)
    mi = MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
    inp = INPUT(0, mi)
    SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

def get_cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def get_foreground_title():
    if not allowed_window_title_substrings:
        return None, None, True  # 无限制
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None, False
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value or ""
    ok = any(s in title for s in allowed_window_title_substrings)
    return hwnd, title, ok

def paused_wait_loop(paused_msg="⏸ 已暂停，按 F6 恢复；Esc 退出"):
    printed = False
    while True:
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            print("▶ 检测到 Esc，退出")
            raise KeyboardInterrupt
        if user32.GetAsyncKeyState(VK_F6) & 0x8000:
            # 防抖
            time.sleep(0.3)
            print("▶ 恢复")
            return
        if not printed:
            print(paused_msg)
            printed = True
        time.sleep(0.1)

# 拟人移动（贝塞尔曲线 + 抖动 + 偶尔停顿）
def human_move(x, y):
    cx, cy = get_cursor_pos()
    steps = random.randint(18, 30)

    # 两个控制点：一个靠近起点、一个靠近终点，带随机偏移（更自然）
    ctrl1 = (cx + random.randint(-60, 60), cy + random.randint(-60, 60))
    ctrl2 = (x  + random.randint(-60, 60), y  + random.randint(-60, 60))

    for i in range(1, steps + 1):
        t = i / steps
        # 三阶贝塞尔
        nx = (1-t)**3 * cx + 3*(1-t)**2*t*ctrl1[0] + 3*(1-t)*t**2*ctrl2[0] + t**3*x
        ny = (1-t)**3 * cy + 3*(1-t)**2*t*ctrl1[1] + 3*(1-t)*t**2*ctrl2[1] + t**3*y
        # 轻微抖动
        nx += random.uniform(-1.5, 1.5)
        ny += random.uniform(-1.5, 1.5)
        send_mouse_abs(int(nx), int(ny), MOUSEEVENTF_MOVE)
        time.sleep(0.01)
        # 偶尔中途停顿一下
        if random.random() < 0.04:
            time.sleep(random.uniform(0.03, 0.15))

def human_click(x, y):
    human_move(x, y)
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTDOWN)
    time.sleep(random.uniform(0.05, 0.15))  # 按压时间
    send_mouse_abs(x, y, MOUSEEVENTF_LEFTUP)

# =============== 主流程 ===============
def main():
    # 日志初始化
    writer = None
    if LOG_TO_FILE:
        exists = os.path.exists(LOG_PATH)
        f = open(LOG_PATH, "a", newline="", encoding="utf-8")
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["ts", "x", "y", "batch_idx", "click_in_batch", "note"])

    print(f"即将开始，{countdown} 秒倒计时…（F6 暂停/继续，F8 立刻大休息，Esc 退出）")
    time.sleep(countdown)
    print("开始点击！")

    start_time = time.time()
    last_reset = start_time
    next_reset_interval = random.uniform(*reset_interval_range)

    try:
        while True:
            now = time.time()
            elapsed = now - start_time
            if elapsed >= run_duration:
                print("✅ 达到运行时长，自动结束。")
                break

            # 全局热键：Esc 退出、F6 暂停
            if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                print("▶ 检测到 Esc，退出。")
                break
            if user32.GetAsyncKeyState(VK_F6) & 0x8000:
                time.sleep(0.3)
                paused_wait_loop()

            # 前台窗口保护（可选）
            _, title, ok = get_foreground_title()
            if not ok:
                paused_wait_loop("⏸ 前台窗口不匹配白名单，按 F6 继续；Esc 退出")

            # 是否进入节奏重置的“超长休息”
            if (now - last_reset) > next_reset_interval:
                rest = random.uniform(*reset_rest_range)
                print(f"🧘 节奏重置：超长休息 {rest/60:.1f} 分钟…")
                time.sleep(rest)
                last_reset = time.time()
                next_reset_interval = random.uniform(*reset_interval_range)

            # 采样本轮点击次数
            batch = sample_batch()

            # 为本轮设定“中心 + sigma”，sigma 每轮微调，避免固定
            cx = region[0] + region[2] // 2
            cy = region[1] + region[3] // 2
            sigma_x = region[2] / random.uniform(5.5, 7.5)
            sigma_y = region[3] / random.uniform(5.5, 7.5)

            for i in range(batch):
                # 热键轮询
                if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                    print("▶ 检测到 Esc，退出。")
                    raise KeyboardInterrupt
                if user32.GetAsyncKeyState(VK_F6) & 0x8000:
                    time.sleep(0.3)
                    paused_wait_loop()
                # F8 立即大休息
                if user32.GetAsyncKeyState(VK_F8) & 0x8000:
                    time.sleep(0.3)
                    rest = random.uniform(*rare_rest_range)
                    print(f"😴 手动触发大休息 {rest:.1f}s…")
                    time.sleep(rest)

                # 生成目标点（高斯分布），再加微小偏移
                x = int(random.gauss(cx, sigma_x)) + random.randint(-micro_offset_px, micro_offset_px)
                y = int(random.gauss(cy, sigma_y)) + random.randint(-micro_offset_px, micro_offset_px)

                # 限制到区域内
                x = clamp(x, region[0], region[0] + region[2])
                y = clamp(y, region[1], region[1] + region[3])

                human_click(x, y)

                if writer:
                    writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), x, y, int(elapsed//60), i+1, ""])
                rem_min = (run_duration - (time.time()-start_time))/60
                print(f"点击: ({x}, {y}) | 已运行 {elapsed/60:.1f} 分 | 剩余 {rem_min:.1f} 分")

                # 本轮内部连点间隔
                if i < batch - 1:
                    time.sleep(random.uniform(*intra_click_sleep_range))

            # 一轮完成 → 休息（偶尔大休息）
            if random.random() < rare_rest_prob:
                rest = random.uniform(*rare_rest_range)
                print(f"😴 偶尔大休息 {rest:.1f} 秒…")
            else:
                rest = random.uniform(*normal_rest_range)
                print(f"休息 {rest:.1f} 秒后进入下一轮…")
            time.sleep(rest)

    except KeyboardInterrupt:
        print("⏹ 手动中断。")
    finally:
        try:
            if LOG_TO_FILE:
                f.close()
                print(f"📄 日志已写入：{os.path.abspath(LOG_PATH)}")
        except Exception:
            pass

if __name__ == "__main__":
    main()
