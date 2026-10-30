# 通用播放器 v2

基于 ESP32 iPhone Agent by Henry Yuan (MIT)，保留根 LICENSE/NOTICE。

一次安装固件，之后通过COM上传独立AMR2数据；不把项目坐标编入程序。支持原来的350ms定位、60ms按压和至少90ms释放，最短500ms间隔。复用原ReplayEngine；本目录副本必须与offline_replay/replay_engine.h保持一致。

工作台生成时自动复制到独立任务目录并编译；可选secrets.h配置电脑录制的Wi-Fi与服务器地址。没有secrets.h则完全离线，不发起Wi-Fi连接。修改网络配置需重新安装固件，修改录制无需重刷。

串口115200命令与AMR2格式见根docs/architecture.md。固件严格校验长度、版本、CRC、设备方向/尺寸、坐标和时序。双NVS槽写非活动槽并读回，最后切active键；写入前先持久disarm。commit不启用，arm是单独操作。没有录制或数据损坏时不执行。

上传/启用/切desktop前必须从移动设备拔USB/HID；固件同时检查挂载状态和挂载事件代数，上传中重连会中止。Serial0是COM管理口，原生USB是单一绝对鼠标及键盘。打开串口可能复位，不作为无干扰观察手段。

启用后的普通上电可自动循环；软件/看门狗复位保持hold。BOOT停止，松开不恢复；物理重插可开始新会话。休眠、HID发送失败停止，后续恢复先补零报告释放。网页停止不控制板内循环。

电脑模式沿用TCP hello absolute及旧指令；离线模式关闭网络，不让Wi-Fi连接等待影响回放。电脑模式TCP重连可能阻塞约100ms，HID发送超时也会影响停止响应；不是硬实时安全设备。

测试：`tools/test_player.cpp`覆盖纯C++解析、双槽写入故障、断电模型、USB门禁、超时/溢出、原时序；Python tests/test_player_native.py将真实Python编码/传输连到同一C++核心。宿主模拟和真实Arduino编译不代替实板NVS断电、USB/iPad落点与Wi-Fi验收。
