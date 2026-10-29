# UxPlay 本机改动

上游：https://github.com/FDH2/UxPlay

基线提交：`ec203121355220dd90278a369eeabb0b8ba4f14a`。本机使用该源码重新构建的 UxPlay，额外补丁只在 NTP 发送失败时输出目标地址，便于定位 IPv6 连接问题，不改变 HID 或图像控制逻辑。

`ntp-diagnostics.patch` 保存相对上述基线的全部源码改动，适用于重新获取相同上游版本后恢复。本仓库不包含编译器、Homebrew 依赖及二进制；构建要求遵循 UxPlay 官方 README。上游代码及此衍生补丁遵循原 GPL 许可。

旧验证主机使用过 `build-desktop-diagnostic/uxplay`。新机器在 `server/.env` 的 `UXPLAY` 指定实际路径，或使用 PATH 中的 `uxplay`，不要绑定他人电脑的绝对目录。
