from car_test.netprobe import SSHClient

def main():

    ssh = SSHClient(host = "192.168.1.201",user="root",password="oelinux123")

    try:
        ssh.connect()
        print("Connected")

        rc,out,err= ssh.exec_cmd("ls /")
        print("返回码:", rc)
        print("输出:", out)
        print("错误:", err)

        if ssh.ping("192.168.1.99"):
            print("远端 ping ok")
        else:
            print("远端 ping fail")

    except Exception as  e:
        print("出错:",e)

    finally:
        ssh.close()
        print("ssh已关闭")

if __name__ == '__main__':
    main()