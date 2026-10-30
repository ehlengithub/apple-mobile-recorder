# 通用播放器 v2

基于 ESP32 iPhone Agent by Henry Yuan (MIT)，保留根 LICENSE/NOTICE。

USB-S3与经典ESP32/BLE共用player.ino、存储和调度，工作台按控制器选择编译。BLE使用-AMR_BLE=1、NimBLE-Arduino2.5.1，USB分支不引入NimBLE。一次安装固件，之后通过COM上传独立AMR2数据；不把项目坐标编入程序。支持原来的350ms定位、60ms按压和至少90ms释放，最短500ms间隔。复用原ReplayEngine；本目录副本必须与offline_replay/replay_engine.h保持一致。

工作台生成时自动复制到独立任务目录并编译；可选secrets.h配置电脑录制的Wi-Fi与服务器地址。没有secrets.h则完全离线，不发起Wi-Fi连接。修改网络配置需重新安装固件，修改录制无需重刷。

串口115200命令与AMR2格式见根docs/architecture.md。固件严格校验长度、版本、CRC、设备方向/尺寸、坐标和时序。双NVS槽写非活动槽并读回，最后切active键；写入前先持久disarm。commit不启用，arm是单独操作。没有录制或数据损坏时不执行。

USB上传/启用/切desktop前必须拔USB/HID；BLE须在打开串口前保持供电长按BOOT两秒进入持久维护，断开连接且不广播。固件逐命令检查维护持久化、连接状态和管理事件代数，上传中连接会中止。Serial0为管理口；USB是绝对鼠标+键盘，BLE仅绝对鼠标，保持旧版描述符和7字节报告。打开串口可能复位，不作为无干扰观察手段。

启用后的普通上电可自动循环；软件/看门狗复位保持hold。BOOT停止，松开不恢复；USB物理重插或BLE重连可开始新会话；BLE长按BOOT进入持久维护，重启仍不执行。明确arm/desktop退出维护后才恢复广播。休眠、HID发送失败停止，后续恢复先补零报告释放。网页停止不控制板内循环。

电脑模式沿用TCP hello absolute及旧指令；离线模式关闭网络，不让Wi-Fi连接等待影响回放。电脑模式TCP重连可能阻塞约100ms，HID发送超时也会影响停止响应；不是硬实时安全设备。

测试：`tools/test_player.cpp`覆盖纯C++解析、双槽写入故障、断电模型、USB/BLE门禁、持久维护、启用失败/断电模型、BLE队列有界重试与主/管理会话事件隔离、超时/溢出、原时序；Python tests/test_player_native.py将真实Python编码/传输连到同一C++核心。宿主模拟和真实Arduino编译不代替实板NVS断电、USB/iPad落点与Wi-Fi验收。

BLE兼容逻辑来自原部署仓库a790fd2。iOS描述符兼容性参考[pilot](https://github.com/ethandgoodhart/pilot)；旧版实测摘要见docs/validation/ble-legacy.json，新整合版须重新实机验收。
