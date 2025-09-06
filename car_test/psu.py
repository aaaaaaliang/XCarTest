import serial
import time
'''
 SCPI协议
 
1. 基本配置命令

    设置电压
    VOLT 12.0
    VOLT 5.5
    VOLT?      // 查询设定电压

    设置电流限值
    CURR 2.0
    CURR 0.5
    CURR?      // 查询设定电流

2. 输出控制命令
    打开输出
    OUTP ON
    
    关闭输出
    OUTP OFF
    
    查询输出状态
    OUTP?

3. 测量命令
    测量电压
    MEAS:VOLT?
    
    测量电流
    MEAS:CURR?
    
    测量功率
    MEAS:POW?

4. 系统类命令
    设备识别
    *IDN?      // 返回厂家、型号、序列号、固件版本
    
    复位设备
    *RST
    
    清除状态
    *CLS
    
    自检
    *TST?

'''

""" 
请根据实际使用串口修改， 波特率和超时一般不做修改

"""
class OwonPSU:
    def __init__(self,port = "COM3",baud = 115200, timeout=1.0):
        self.ser = serial.Serial(port = port, baudrate = baud, timeout = timeout)

    def write(self,cmd: str):
        """发送 SCPI 命令"""
        self.ser.write((cmd + "\r\n").encode())

    def query(self,cmd: str) -> str:
        """发送查询命令并返回响应"""
        self.write(cmd)
        time.sleep(0.1)
        return  self.ser.read_all().decode().strip()

    def set_voltage(self,v: float):
        """设置电压"""
        self.write(f"VOLT {v}")


    def set_current(self, a: float):
        """设置电流"""
        self.write(f"CURR {a}")

    def on(self):
        """"""
        self.write("OUTP ON")

    def off(self):
        """"""
        self.write("OUTP OFF")

    def measure_voltage(self) -> float:
        resp = self.query("MEAS:VOLT?")
        return float(resp) if resp else 0.0

    def measure_current(self) -> float:
        resp = self.query("MEAS:CURR?")
        return float(resp) if resp else 0.0