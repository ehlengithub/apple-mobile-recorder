# 架构与命令行

## 目录与职责

| 路径 | 职责 |
| --- | --- |
| `server/hidagent/static/studio.*` | 中文工作台：设备档案、点击录制、时间表、生成与烧录 |
| `server/hidagent/studio.py` | 项目校验、隔离构建、端口/板型/分区核对、备份、写入及恢复 |
| `server/hidagent/studio_api.py` | 工作台 HTTP 接口 |
| `server/hidagent/macro.py`、`phone.py`、`vision.py` | 原电脑录制、图像校验、绝对定位与点击保护 |
| `hardware/hid_bridge` | 录制模式固件，Wi-Fi 接受电脑指令，USB 绝对鼠标执行 |
| `hardware/offline_replay` | 通用离线执行器；仓库默认头文件不含任何可执行录制 |
| `examples` | 独立设备项目格式和安全测试页 |

录制项目用标准化坐标0～1，生成固件时转为0～32767的USB绝对坐标。它解决相对鼠标累计偏移，但不对跨设备页面布局做推理。

生成时把固件源文件和项目头文件复制到本机任务目录，再编译独立二进制。不会在共享源码中反复改用户录制，也不会把某一个录制SHA256写成全局使用条件。项目保存包含设备档案，修改档案需要另建录制。

## 本机数据

```text
~/.apple-mobile-recorder/studio/
  projects/<project-id>/
    project.json       # 可导出的步骤、档案和时序
    previous.json      # 上次保存的项目
    recording.json     # 可选，本机原始图像校验依据；不对外导出
  jobs/<job-id>/
    job.json           # 阶段、结果、错误、限定长度的日志
    project.json       # 本次编译的不可变快照
    manifest.json      # 二进制哈希、模式、周期、轮数和部署编号
    OfflinePlayer/    # 私有、独立的固件源文件
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

普通写入仅应用区0x10000；先备份启动区0～0xffff和可信分区表指定的应用区。任务之间保持ROM下载模式，最终写入通过哈希校验后才硬复位。空板初始化只在用户指定且启动区全空时允许，写入范围仍先备份。恢复核对MAC和快照哈希，不支持把一块板子的备份直接克隆到另一块。

离线固件的`macro_data.h`由项目生成；仓库模板`MACRO_COUNT=0`，直接编译不会执行任何用户坐标。有限轮数结束后释放按键并停止；0表示持续循环。每次生成的新部署有持久待机锁，需在COM供电下观察一次原生USB断开后重插启用。按BOOT停止，EN硬复位可能重启循环。

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
