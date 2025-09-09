from udsoncan import DataIdentifier, DidCodec
import isotp
import can
import udsoncan
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.client import Client
from udsoncan import configs

class VINCodec(DidCodec):
    def encode(self,val: str) -> bytes:
     data = val.encode('ascii')
     if len(data) != 17:
         raise ValueError("VIN 必须是17字节")
     return data



class UdsClientWrapper:
    """
    基于 python-can + isotp + udsoncan 的 UDS 客户端封装

    用法示例:
        with UdsClientWrapper(interface="vector", channel=0, bitrate=500000,
                              tx_id=0x7E0, rx_id=0x7E8) as client:
            # 进入扩展会话 (SID=0x10, SubFunction=0x03)
            client.change_session(udsoncan.services.DiagnosticSessionControl.Session.extendedDiagnostic)

            # 读取 VIN (DID = 0xF190)
            response = client.read_data_by_identifier(udsoncan.DataIdentifier.VIN)
            print("VIN:", response.service_data.values)
    """

    def __init__(self,
                 interface: str = "vector",    # CAN 接口类型 (vector/pcan/socketcan/kvaser...)
                 channel: int = 0,             # 通道号 (Vector: 0=CAN1, 1=CAN2)
                 bitrate: int = 500000,        # 波特率
                 app_name: str = "CANoe",      # Vector 的 Application name
                 tx_id: int = 0x7E0,           # 请求报文 CAN ID
                 rx_id: int = 0x7E8            # 响应报文 CAN ID
                 ):
        # 1) 初始化 CAN 总线
        kwargs = dict(interface=interface, channel=channel, bitrate=bitrate)
        if interface == "vector":
            kwargs["app_name"] = app_name
        self.bus = can.interface.Bus(**kwargs)

        # 2) ISO-TP 地址配置
        addr = isotp.Address(isotp.AddressingMode.Normal_11bits,
                             txid=tx_id,
                             rxid=rx_id)

        # 3) ISO-TP 协议栈参数
        isotp_params = {
            "stmin": 0,              # ECU 连续帧最小间隔 (ms)
            "blocksize": 8,          # 连续帧个数
            "ll_data_length": 8,     # 单帧长度 (标准 CAN=8, FD 可到 64)
            "tx_data_length": 8,     # 发送数据长度
            "rx_flowcontrol_timeout": 1000,       # 等待FC超时(ms)
            "rx_consecutive_frame_timeout": 1000  # 等待CF超时(ms)
        }

        # 4) 创建 ISO-TP stack
        stack = isotp.CanStack(bus=self.bus, address=addr, params=isotp_params)

        # 5) 传输层封装
        conn = PythonIsoTpConnection(stack)

        # 6) UDS 客户端配置
        my_cfg = configs.default_client_config.clone()
        my_cfg["request_timeout"] = 2          # 等待响应超时 (s)
        my_cfg["p2_timeout"] = 1               # P2 时间 (s)
        my_cfg["p2_star_timeout"] = 5          # P2* 时间 (s)
        my_cfg["s3_client"] = 5                # 会话保持 (客户端)
        my_cfg["s3_server"] = 5                # 会话保持 (服务端)

        my_cfg['data_identifiers'] = {
            DataIdentifier.VIN: VINCodec(),
        }

        my_cfg["security_algo"] = self.my_security_algo

        # 👉 后续可以在这里加 security_algo / data_identifiers 等

        # 7) 创建 UDS Client
        self.client = Client(conn, request_timeout=2, config=my_cfg)

    @staticmethod
    def my_security_algo(level: int, seed: bytes, params=None) -> bytes:
        seed_int = int.from_bytes(seed, byteorder='big')
        """根据真实的计算 例子 大端"""
        key_int = seed_int ^ 0x11111111
        return key_int.to_bytes(len(seed), byteorder='big')


    def __enter__(self):
        """进入 with 时自动打开连接"""
        self.client.open()
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出 with 时自动关闭"""
        self.client.close()
        self.bus.shutdown()


