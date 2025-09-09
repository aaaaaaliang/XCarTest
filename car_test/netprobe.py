from platform import system
import subprocess
import paramiko
from typing import Optional,Tuple
import platform

class SSHClient:
    def __init__(self, host: str, port: int = 22, user: str = "root",
                 password: Optional[str] = None, pkey_path: Optional[str] = None, timeout: int = 15):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.pkey_path = pkey_path
        self.timeout = timeout
        self.client: Optional[paramiko.SSHClient] = None
        self.is_windows: Optional[bool] = None

    def connect(self):
        """建立 SSH 连接"""
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if self.pkey_path:
            key = paramiko.RSAKey.from_private_key_file(self.pkey_path)
            c.connect(self.host, port=self.port, username=self.user, pkey=key, timeout=self.timeout)
        else:
            c.connect(self.host, port=self.port, username=self.user, password=self.password, timeout=self.timeout)
        self.client = c

        # 探测远端 OS
        try:
            rc, out, _ = self.exec_cmd("uname")
            if rc == 0 and out.strip():
                self.is_windows = False
            else:
                self.is_windows = True
        except Exception:
            # uname 不存在，大概率是 Windows
            self.is_windows = True

        # 兜底确认：Windows 系统一般支持 ver
        if self.is_windows:
            try:
                _, out, _ = self.exec_cmd("ver")
                if "Windows" not in out:
                    self.is_windows = False
            except Exception:
                pass


    def exec_cmd(self, cmd: str, timeout: int = 10) -> Tuple[int, str, str]:
        """执行命令，返回 (exit_code, stdout, stderr)"""
        if self.client is None:
            """ 需要捕获 """
            raise RuntimeError("SSH 未连接")
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        """ ignore: 忽略非法字节 """
        return rc, stdout.read().decode("utf-8", "ignore"), stderr.read().decode("utf-8", "ignore")


    def ping(self,ip:str, count: int=1, timeout: int = 3) -> bool:
        if self.is_windows is None:
            raise RuntimeError("未能检测远端 OS，请先调用 connect()")

        if self.is_windows:
            cmd = f"ping -n {count} -w {timeout * 1000} {ip}"
        else:
            cmd = f"ping -c {count} -W {timeout} {ip}"

        rc,out,_ = self.exec_cmd(cmd = cmd,timeout = timeout + 2)
        return  rc == 0

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

def ping_local(ip: str, count: int = 1, timeout: int = 3) -> Tuple[bool,str]:
    """
    在本机执行 ping 命令
    :param ip: 目标 IP 地址
    :param count: 发送次数
    :param timeout: 超时时间 (秒)
    return: (是否成功, 输出文本)
    """

    system = platform.system().lower()
    if system.startswith("win"):
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), ip]

    try:
        proc = subprocess.run(
            cmd,
            stdout= subprocess.PIPE,
            stderr= subprocess.STDOUT,
            text=True,
            timeout = timeout+1,
        )
        ok = (proc.returncode == 0)
        return ok,proc.stdout.strip()

    except Exception as e:
        return False,f"Ping 执行异常: {e}"