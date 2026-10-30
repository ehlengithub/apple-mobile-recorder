# 蓝牙版接入工作台

## 已合并的范围

经典ESP32（ESP32-D0WDQ6，4MB）使用 `ble-esp32` 控制器；S3/USB使用 `usb-s3`。相同player.ino、AMR2格式、双NVS槽、回放引擎和时间表，仅HID驱动、编译配置和管理门禁不同。蓝牙旧版来源为原部署仓库a790fd2，继承原MIT来源说明。

旧版固定macro_data.h的录制不直接复制进新固件。首次安装通用播放器后，从工作台项目上传AMR2；更新录制无需编译或重刷，也不会改HID描述符或清除蓝牙bond命名空间。

## 维护模式

连接电脑串口可能复位，蓝牙即使没有USB/HID线仍能点击iPad。管理前应保持板子供电，长按BOOT至少两秒，进入持久维护：关闭输入、停止广播、断开连接、持久关闭自动循环。新固件默认维护，初次安装不会自动继承旧版auto设置。

UI的“已进入维护”复选框用于确认打开串口的前提；后端仍检查MAC、transport=ble、chip=esp32、maintenance=true、maintenance_saved=true、connected=false。板端逐条检查门禁与连接事件代数；没有维护、发生连接、存储失败或校验错误时拒绝更新。

上传后保持维护和待机。启用前先把iPad放到正确起始页。明确arm后才退出持久维护、广播并重连，倒计时后执行。切电脑模式同样退出维护并恢复广播，电脑模式需要首次配置的Wi-Fi；自动循环不需要Wi-Fi。

短按BOOT停止当前会话，长按进入持久维护；不要按住BOOT再复位，以免进入ROM下载模式。普通上电可能自动执行已启用录制；EN不是停止。iPad不发送HID休眠指令时，板子不能单独判断其锁屏/页面状态。

## BLE兼容性

保持已验收的BLE_ABSOLUTE_MAP、report ID1、7字节绝对鼠标、0..32767坐标、名字iPad BLE Recorder、PnP和HIDInfo。iPad缓存描述符，若配对异常再忽略旧设备并重新配对；不承诺更新固件后所有机型都无需配对。

只有连接、加密、报告订阅、报告模式正确且未休眠才可发送。notify有界重试，失败停止，不把队列接受当作页面成功。重连从第一步倒计时，暂停不半轮续跑。BLE不添加键盘描述符，键盘指令明确拒绝；主工作台只录点击。

## CLI

首次安装后，先在板上完成持久维护，再运行：

```sh
server/.venv/bin/python -m hidagent.workbench build examples/ipad-six-step.json --controller ble-esp32
server/.venv/bin/python -m hidagent.workbench flash BUILD_ID --controller ble-esp32 --port /dev/cu.YOUR_PORT
server/.venv/bin/python -m hidagent.workbench upload examples/ipad-six-step.json --controller ble-esp32 --port /dev/cu.YOUR_PORT --mac YOUR_CHIP_MAC --maintenance-confirmed
server/.venv/bin/python -m hidagent.workbench player arm --controller ble-esp32 --project examples/ipad-six-step.json --port /dev/cu.YOUR_PORT --mac YOUR_CHIP_MAC --maintenance-confirmed
```

CLI build默认不含网络，首次需要电脑录制应通过工作台生成含Wi-Fi配置的播放器。所有示例端口/MAC须替换为自己的板卡；MAC使用烧录识别的芯片MAC，不用移动设备的蓝牙地址。

## 证据与边界

旧版在iPad14,3完成27/27九点测试、六步循环、13轮日志和普通电源恢复确认；摘要见 `validation/ble-legacy.json`。本次整合版的模拟传输、存储故障和真实编译另记 `validation.md`；未烧录、未完成新版落点与独立供电验收，不能沿用旧版结论当作新版本验收。
