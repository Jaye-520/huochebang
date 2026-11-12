# 项目记忆

- `x001` 目标是 OCR + Android ADB 控制。`ADBController` 的默认输入通道是 `adb shell input`；对货车帮司机这类 App 必须显式传 `input_backend="uiautomator"`，不要自动回退，避免同一点击被重复发送。
- 真机基线：Xiaomi MIX 3 / Android 10，分辨率 1080x2340，内置 platform-tools 37.0.1。测试时保持 `stay_on_while_plugged_in=7`。
- 货车帮司机 `com.wlqq` 对 shell 注入触摸通常不响应，但 UIAutomator 后端可用；UIAutomator dump 在该 App 可能返回 null root。
