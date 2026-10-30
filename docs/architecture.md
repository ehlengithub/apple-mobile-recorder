# 架构与命令行

## 目录与职责

| 路径 | 职责 |
| --- | --- |
| `server/hidagent/static/studio.*` | 中文工作台：设备档案、点击录制、时间表、生成与烧录 |
| `server/hidagent/studio.py` | 项目校验、隔离构建、端口/板型/分区核对、备份、写入及恢复 |
| `server/hidagent/studio_api.py` | 工作台 HTTP 接口 |
| `server/hidagent/macro.py`、`phone.py`、`vision.py` | 原电脑录制、图像校验、绝对定位与点击保护 |
| `server/hidagent/player.py`、`player_api.py` | AMR2数据编码、串口传输、独立上传与启用接口 |
| `hardware/player` | 通用播放器v2；双槽录制存储、离线执行、可选Wi-Fi电脑录制 |
| `hardware/hid_bridge` | 录制模式固件，Wi-Fi 接受电脑指令，USB 绝对鼠标执行 |
| `hardware/offline_replay` | 通用离线执行器；仓库默认头文件不含任何可执行录制 |
| `examples` | 独立设备项目格式和安全测试页 |

录制项目用标准化坐标0～1，导出录制文件时转为0～32767的USB绝对坐标。它解决相对鼠标累计偏移，但不对跨设备页面布局做推理。

通用播放器v2编译不包含任何项目坐标。首次安装固件后，录制通过COM独立上传到NVS双槽，改录制或时序无需重编。Wi-Fi配置是可选的首次构建配置；配置后同一固件可切电脑录制/离线执行。旧hardware/offline_replay、hid_bridge保留为历史参考，不是v2默认构建。

AMR2格式：小端44字节头 `<4sHHIIIHHBBH16s>`，依次为魔数AMR2、版本2、步骤数、倒计时ms、轮数、释放ms、宽、高、方向、设备类型、保留0、项目UUID16字节；随后每步 `<HHI>` 为绝对x/y与间隔ms，末尾uint32 CRC32覆盖之前所有字节。总长 `44+8*N+4`，1～256步，最多2096字节。不含图像、名称或网络凭据。CRC用于传输损坏检测，不是身份认证。

板端先持久关闭自动执行，写非活动槽、读回并验证格式与CRC，再原子切活动槽。中断不覆盖旧活动槽；重启只加载已提交槽，损坏数据不执行。上传完成仍待机，明确arm后才在下次USB连接与倒计时后执行。

## 串口传输

Serial0 115200，一行一个命令；响应为 `AMR2 {JSON}`。`info`核对协议/MAC/挂载状态；`read offset length`读回最多128字节；`begin size crc`、`data offset hex`、`commit`上传；`arm`启用、`desktop`切电脑录制、`stop`停止、`abort`放弃传输。

所有修改操作要求USB/HID已从移动设备断开，固件逐命令检查；工作台也要求用户确认断开并核对MAC。打开COM可能引发板卡复位，所以不能仅依赖主机“不切换DTR”设置。发送失败、偏移不一致、无效数据、CRC或读回不符都不会报告成功。启用前还核对板内数据与当前项目的CRC及完整内容。

## 本机数据

```text
~/.apple-mobile-recorder/studio/
  projects/<project-id>/
    project.json       # 可导出的步骤、档案和时序
    previous.json      # 上次保存的项目
    recording.json     # 可选，本机原始图像校验依据；不对外导出
  jobs/<job-id>/
    job.json           # 阶段、结果、错误、限定长度的日志
    project.json       # 本次上传的项目快照
    manifest.json      # 二进制哈希、协议、网络配置标记及构建编号
    UniversalPlayer/  # 独立构建，不含项目录制
    recording.amr     # 上传的纯录制数据
    previous.amr      # 上传前读回的旧录制（若存在）
    firmware/         # 二进制及Arduino构建结果
    backup/           # 烧录前应用和启动区、恢复元数据
```

Mac/Linux上根数据目录权限为0700。跨进程文件锁阻止同一数据目录同时做两次编译/烧录。串口被其他程序占用时不会结束其他程序。工作台Web只监听127.0.0.1，拒绝跨站修改请求；板端控制TCP默认监听7400供同局域网板子连接。

## 命令行

在项目根目录使用自己的虚拟环境，不依赖全局安装：

```sh
server/.venv/bin/python -m hidagent.workbench doctor
server/.venv/bin/python -m hidagent.workbench serve
server/.venv/bin/python -m hidagent.workbench import examples/ipad-six-step.json
server/.venv/bin/python -m hidagent.workbench build examples/ipad-six-step.json
server/.venv/bin/python -m hidagent.workbench flash BUILD_ID --port /dev/cu.YOUR_COM_PORT
server/.venv/bin/python -m hidagent.workbench restore FLASH_JOB_ID --port /dev/cu.YOUR_COM_PORT
```

`doctor`只检查环境与枚举端口，不会打开COM控制板子。`--data /path`须放在子命令前，可隔离不同使用者或测试数据。`serve --without-receiver --no-browser`适合外部独立接收器；通过`MIRROR_STREAM=127.0.0.1:PORT`指定画面来源。

主工作台默认8990，独立接收器8991，板端控制7400。旧项目8890/8891/7300与其分别配置，不要混用板端目标端口。`server/.env`可设置`HIDAGENT_DATA`、`AIRPLAY_NAME`、`DEVICE_PORT`、`MIRROR_PORT`和`UXPLAY`。

## 便携项目格式

见`examples/*.json`。字段包括`schema_version`、`id`、`name`、`device`、`steps`、`loops`、`startup_ms`。每一步当前仅支持`do: tap`、`x`、`y`、`wait_ms`。不支持的动作直接拒绝，不会静默丢弃。归一化坐标、有限数值、方向与画面比例、步骤数量及物理时间下限都有校验。

外部导入项目获得新的项目ID，不能覆盖本机原图校验依据。导入的坐标计划可以生成离线固件，但不能直接当作拥有图像依据的电脑试跑录制。要做电脑严格试跑，须在对应设备上重新录制。

## 构建与恢复

参考 FQBN：`esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=8M`。Arduino core固定3.3.12，Python依赖由`server/uv.lock`固定。

普通写入仅应用区0x10000；先备份启动区0～0xffff和可信分区表指定的应用区。任务之间保持ROM下载模式，最终写入通过哈希校验后才硬复位。空板初始化只在用户指定且启动区全空时允许，写入范围仍先备份。恢复核对MAC、快照哈希及当前分区表，不支持把一块板子的备份直接克隆到另一块。

播放器固件和录制是两个产物。旧版按项目生成的固件可继续用原备份恢复；v2新安装后需要单独上传、启用。固件恢复不会回退NVS内录制，请从本机原项目重新上传；previous.amr作为旧数据备份留在任务目录；不能把应用区快照当成完整Flash/NVS克隆。

CLI独立上传示例（先拔USB/HID；MAC须来自自己的已核对板卡）：

```sh
server/.venv/bin/python -m hidagent.workbench export-recording examples/ipad-six-step.json --output /tmp/example.amr
server/.venv/bin/python -m hidagent.workbench upload examples/ipad-six-step.json --port /dev/cu.YOUR_COM_PORT --mac YOUR_BOARD_MAC --hid-disconnected
server/.venv/bin/python -m hidagent.workbench player arm --project examples/ipad-six-step.json --port /dev/cu.YOUR_COM_PORT --mac YOUR_BOARD_MAC --hid-disconnected
server/.venv/bin/python -m hidagent.workbench player desktop --port /dev/cu.YOUR_COM_PORT --mac YOUR_BOARD_MAC --hid-disconnected
```

工作台历史接口 `GET /api/studio/jobs`在刷新/进程重启后从本机元数据重建任务和备份列表；已有日志接口仍按任务ID读取。轮询暂时失败会重试，不丢弃任务ID。电脑试跑路由使用async，在服务事件循环中调度真实Macro。

## 开发验证

```sh
uv sync --project server --all-extras --frozen
HIDAGENT_DATA=/tmp/apple-recorder-test server/.venv/bin/python -m pytest server/tests -q
server/.venv/bin/ruff check server/hidagent/studio.py server/hidagent/studio_api.py server/hidagent/workbench.py
node --check server/hidagent/static/studio.js
c++ -std=c++17 tests/replay_engine_test.cpp -o /tmp/replay-engine-test
/tmp/replay-engine-test
```

macOS某些SDK版本的C++头文件需显式`-isysroot`到本机可用的CommandLineTools SDK，不能将他人机器的SDK路径写死。浏览器UI测试使用独立数据目录；测试前备份将触碰的localStorage/sessionStorage，测试后恢复字段、折叠等持久状态。本工作台本身不写浏览器存储。
