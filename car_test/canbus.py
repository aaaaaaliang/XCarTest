import can
import time



"""
只是封装了简单的封装，可以自己在调用的时候处理收发逻辑
"""
class CanBus:
    def __init__(self, interface="vector", channel=0, bitrate=500000, app_name="CANoe"):
        """
        初始化 CAN 通道
        :param interface: 接口类型 (vector, pcan, socketcan, kvaser...)
        :param channel: 通道号 (Vector: 0=CAN1, 1=CAN2)
        :param bitrate: 波特率
        :param app_name: Vector 使用的应用名 (CANoe / CANalyzer)
        """
        kwargs = dict(interface=interface, channel=channel, bitrate=bitrate)
        if interface == "vector":
            kwargs["app_name"] = app_name
        self.bus = can.Bus(**kwargs)

    def collect_ids(self, sample_s: float = 3.0) -> set[int]:
        """
        在 sample_s 秒内抓取 CAN 报文 ID
        :return: set[int] (抓到的 ID 集合)
        """
        reader = can.BufferedReader()
        notifier = can.Notifier(self.bus, [reader], timeout=0.1)

        """ 针对相同id 去重 """
        seen = set()
        t0 = time.time()
        while time.time() - t0 < sample_s:
            msg = reader.get_message()
            if msg:
                seen.add(msg.arbitration_id)
        notifier.stop()
        return seen

    def send_frame(self, can_id: int, data: bytes):
        """
        发送一帧 CAN 报文
        """
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)
        self.bus.send(msg)

    def close(self):
        """关闭 CAN 通道"""
        self.bus.shutdown()





