from car_test.netprobe import ping_local

ok,out = ping_local("192.168.1.1")
print("结果","成功" if ok else "失败")
print(out)
