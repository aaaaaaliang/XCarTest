# XCarTest

XCarTest 是一个用于 **车载测试** 的 Python 框架，主要功能：

- ⚡ **电源控制**：基于 SCPI 协议（如 Owon 电源），支持电压、电流设置与测量。  
- 🚗 **CAN 总线操作**：基于 [python-can](https://python-can.readthedocs.io/)，支持报文收发与 ID 抓取。  
- 🔍 **电源 x CAN 联动测试**：在不同电压点位下验证 TBOX 外发报文是否符合预期。  
- 🔄 **保持唤醒**：支持后台周期发送 0x795 报文，维持 ECU 唤醒状态。  

---

## 📦 安装

### 1. 克隆仓库
```bash
git clone https://github.com/aaaaaaliang/XCarTest.git
cd XCarTest
2. 创建虚拟环境（推荐）
bash
复制代码
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate
3. 安装依赖
bash
复制代码
pip install -e .
依赖包括：

pyserial

python-can

🚀 快速开始
电源测试
bash
复制代码
python examples/demo_power_basic.py
CAN 测试
bash
复制代码
python examples/demo_can_basic.py
电源 x CAN 联动验证
bash
复制代码
python examples/demo_power_can.py
保持唤醒
bash
复制代码
python examples/demo_keep_awake.py
📂 项目结构
bash
复制代码
XCarTest/
├── car_test/                  # 主代码（核心库）
│   ├── __init__.py            # 对外暴露接口
│   ├── psu.py                 # 电源控制（SCPI 封装）
│   ├── canbus.py              # CAN 操作（python-can 封装）
│   └── tester.py              # 测试逻辑（整合 psu + canbus）
├── examples/                  # 示例脚本（每个都可直接跑）
│   ├── demo_power_basic.py
│   ├── demo_can_basic.py
│   ├── demo_power_can.py
│   └── demo_keep_awake.py
├── tests/                     # 单元测试
│   └── test_tester.py
├── README.md                  # 使用说明
└── pyproject.toml             # 项目配置 & 依赖管理
📌 TODO
 支持更多电源型号（Rigol、Keysight 等）

 支持更多 CAN 接口（PCAN、SocketCAN 等）

 增加 UDS 诊断服务（0x22、0x27 等）

 增加报文周期/信号级别验证

 集成 pytest 自动化测试

