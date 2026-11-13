# 项目结构与开发指南

## 项目职责

X001 在本地 Android 手机上浏览货车帮司机货源，保存列表和详情截图，以 OCR 提取货源字段，并将采集任务、识别结果和复核信息保存在 SQLite。网页操作台读取同一数据库，提供筛选、分析、截图查看、运行控制和 Excel 导出。

采集依赖真实设备界面与屏幕布局；自动化测试覆盖纯逻辑、数据库和 HTTP 请求校验，不代表已在所有手机型号或应用版本上完成真机验证。

## 主要模块

| 路径 | 职责 |
| --- | --- |
| `src/x001/adb.py` | ADB 与 UiAutomator 设备连接、截图、点击和滚动手势。 |
| `src/x001/collect.py` | 线路检查、卡片遍历、详情截图、任务队列和 OCR 子进程协调。 |
| `src/x001/vision.py` | 截图裁切、线路页识别、卡片匹配与跨屏去重。 |
| `src/x001/freight.py` | 详情字段提取、清洗、遮挡检测和待核对字段管理。 |
| `src/x001/engines.py`、`src/x001/ocr.py` | Windows、RapidOCR/OpenVINO 与 PaddleOCR 后端。 |
| `src/x001/storage.py` | SQLite 表结构、截图编号、任务状态和结果写入。 |
| `src/x001/filters.py` | 网页和 Excel 共用的筛选校验及 SQL 条件。 |
| `src/x001/export.py` | CSV 与 Excel 导出、字段复核提示和输出安全处理。 |
| `src/x001/web.py`、`src/x001/web_static/` | 本地 HTTP 服务、JSON 接口和操作台前端。 |
| `src/x001/launcher.py` | Windows 智能控制中心：准备环境、健康检查、设备预检、固定端口启停网页服务。 |
| `scripts/` | Windows 打包脚本和一次性数据迁移工具。 |
| `tests/` | 使用标准库 `unittest` 的离线测试。 |

## 采集与持久化流程

1. 采集器确认当前线路并将一组截图登记为 `capturing`。
2. 截图组写入磁盘并发布到 `queued` 后，独立 OCR 工作进程读取任务。
3. 识别成功时，`freight` 结果与 `done` 状态一并提交；失败会保留错误状态，识别失败可通过 `--process-only` 重试。
4. `--reprocess-all` 将已有已完成记录重新排队，成功后按采集编号更新结果。
5. 网页直接读取相同的 SQLite 文件；导出只选择 `done` 记录，并保留待核对说明。

默认数据库是 `data/x001.sqlite3`，默认截图位于 `data/captures/<采集编号>/`。自定义数据库的截图目录为同级 `<数据库文件名>-captures/`。截图路径作为相对路径存入数据库；清除截图会移除这些关联，但保留识别结果。

SQLite 的 `CURRENT_TIMESTAMP` 以 UTC 保存。网页把无时区数据库时间解释为 UTC 后转换成本机时间；按日筛选先确定本地日期边界，再转换为 UTC 查询边界。修改时间读写逻辑时应保持此约定。

数据库初始化集中在 `Store.initialize()`，对已有库的兼容变更应采用可重复运行的列检查或迁移，并保留已有采集记录。`exclusive_run()` 用于避免多个进程同时操作同一个数据库或设备。

## 开发环境与检查

- 使用 Python 3.14 及 `uv` 管理环境和锁定依赖。
- Ruff 配置在 `pyproject.toml`：行宽 88，检查 `E`、`F`、`I` 规则。
- 不连接手机即可运行格式、静态检查和单元测试：

```powershell
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
```

Windows 启动器构建命令为 `scripts\build_launcher.ps1`。`scripts/migrate_20261112_text.py` 是针对首批历史记录的一次性迁移，不属于日常运行流程；它会检查目标记录并创建数据库备份，执行前仍应确认当前目录和数据库内容。项目打包时，`web_static/`、`windows_ocr.ps1` 和 `platform-tools/` 属于运行资源，修改打包配置时需确保它们仍包含在发行包内。

## 本地服务注意事项

`x001-web` 默认仅监听回环地址。绑定 `0.0.0.0` 会启用随机 Basic 密码，但服务仍使用未加密 HTTP；只应在可信任的局域网短时使用，不应暴露到公网。服务可操作采集进程、读取截图并导出本地数据库中的数据。

项目数据目录包含个人采集记录和截图，不应提交到版本控制或复制到不可信位置。`.gitignore` 忽略本地数据目录；执行迁移或清理前，先确认数据库路径并保留必要备份。
