# X001 货车帮司机货源采集

X001 从货车帮司机的线路列表读取货源。程序通过 ADB 和 UiAutomator 操作手机界面，保存详情截图，识别货源字段并写入 SQLite。你在手机上选择线路和筛选条件，程序从当前列表开始采集；到达「推荐货源」分区后回顶刷新并继续，直到到达列表末尾或被限量、中断。

## 环境准备

- Python 3.14 或更高版本，使用 uv 安装依赖。
- 默认连接小米 MIX 3，ADB 序列号为 `7450df55`，竖屏分辨率为 `1080 × 2340`。采集器的页面布局和手势以这台设备上的货车帮司机为验证基准。
- 手机开启 USB 调试、保持解锁，并授权电脑连接。项目自带 Windows 版 ADB。其他平台需在 `PATH` 中提供 ADB。
- Windows 默认使用系统简体中文 OCR 做快速初识别。重量或装货时间需要复核时，再用 PP-OCRv5 mobile/OpenVINO 做第二意见；建议只展示，不会自动覆盖原结果或清除复核标记。首次二次识别会从模型仓库下载约 20 MB 权重。显式使用旧 PP-OCRv6 Medium 后端时，Windows 上的 PyTorch 需要 Microsoft Visual C++ 2015–2022 x64 运行库。

在项目根目录安装依赖，确认手机在设备列表中显示为 `device`。

```powershell
uv sync
uv run x001-init
.\src\x001\platform-tools\adb.exe devices -l
```

`x001-init` 会创建本地的 `data/ocr_images/`、`data/ocr_results/`、
`data/captures/` 和 `data/x001.sqlite3`。这些数据路径已加入 Git 忽略规则。
初始化可重复运行，已有图片和货源记录会保留。单图 OCR 命令可直接读取指定路径，
采集命令也会在运行时初始化所需的数据库。
日常采集与导出统一使用 `data/x001.sqlite3`；导出 CSV 是交付表，不需要为每轮采集新建数据库。

## 运行采集

在手机上打开目标线路并设置筛选条件。运行期间让程序独占手机操作。

```powershell
uv run x001-collect
```

首次使用可限制采集量。保存好的截图可在稍后继续识别。

```powershell
uv run x001-collect --max-orders 5
uv run x001-collect --process-only
```

| 参数 | 用途 |
| --- | --- |
| `--database 路径` | 高级选项：指定 SQLite 文件。日常使用省略此参数，统一追加到 `data/x001.sqlite3`。 |
| `--max-orders 数量` | 达到指定采集量后结束本轮，适合真机试跑。 |
| `--capacity 数量` | 限制采集中、排队及识别中的截图组总数，默认 3。达到上限后，手机停在线路列表等待。 |
| `--ocr auto\|windows\|hybrid\|rapid\|paddle` | 默认 `auto` 在 Windows 上使用 Windows 初识别，并按需运行 PP-OCRv5 二次识别；`windows` 只运行 Windows OCR，`hybrid` 强制启用双引擎，`rapid` 单独使用 PP-OCRv5 mobile，`paddle` 保留旧 PP-OCRv6 Medium。 |
| `--process-only` | 只处理当前数据库中待识别、处理失败或上次中断的截图组。 |
| `--reprocess-all` | 用已有截图重新识别和清洗已采集记录，无需连接手机。 |

采集日志会显示线路识别文字。识别完成后，可按该文字导出单线路货源表：

```powershell
uv run x001-export --route 宁夏河北
```

默认生成 `data/x001-freight.csv`（UTF-8 BOM，可直接用 Excel 打开），也可用
`--output` 指定路径。导出仅包含指定线路中已完成识别的记录；缺少关键字段
或识别流程标记的疑义会汇总到「待核对项」，未展示的需要车数显示为「未显示」。
旧库中未记录线路的历史记录不会误混入指定线路；需重新采集后才能按线路导出。

### 遍历与结束条件

- 列表中出现折叠提示时，程序先展开，再按新的页面布局重新定位货源。进详情后分屏截图，返回列表后重新识别卡片。
- 路线文字为已读浅灰色的货源会直接跳过，包括本轮开始前已打开过的货源。当前屏没有可采货源或折叠入口时，程序继续向下浏览列表。
- 程序根据相邻页面重叠的卡片、文字摘要和图像特征减少本轮重复访问。身份有歧义时保留两条。不同运价或装货时间可以分别入库。
- 识别到「推荐货源」分区后，只采集该分区上方的普通货源，不打开推荐区卡片；待上方货源处理完毕，程序回到同一线路顶部、下拉刷新并继续循环。卡片摘要可匹配的本轮已采货源会跳过；达到 `--max-orders` 时停止。
- 遇到列表末尾时结束。连续三次翻页没有进展时，控制台会提示未确认到达末尾。达到 `--max-orders` 时直接结束。

## 截图与数据库

SQLite 视图 `freight_report` 按人工表列序显示货源，查询可用
`SELECT * FROM freight_report ORDER BY 序号`。该视图展示包装、运价口径和备注，
有疑义的字段带「待核对」标记。原图和 OCR 文本仍可通过 `captures`、`freight` 查阅。
已有记录可在采集停止后用 `uv run x001-collect --reprocess-all` 重做清洗。

默认截图位于 `data/captures/<采集编号>/`。自定义数据库文件使用其同目录下的 `<数据库文件名>-captures/`。每组包含列表原图 `card.png` 和按编号排列的详情图 `detail-01.png` 等。

「货源反馈」浮层遮挡字段时，程序先查找已有截图中的完整值，必要时最多短滑补拍两次。补拍文件名包含 `-supplement`。仍无法确认的字段留空，字段名写入 `review_fields`。重量或装货时间被标记复核时，Windows 主结果保持不变；PP-OCRv5 二次意见和原始文字另存到 `freight.second_opinion`，并在导出核对项中明确标为“未自动采用”。若二次引擎初始化或推理失败，错误会单独记录，采集仍保留 Windows 主结果并将相关字段留给人工核对。
装货时间识别会修复 OCR 将时间范围短横线与数字「1」合并识别为「刁」的情况；其他无法确认的时间仍会标记待核对。

| 表 | 内容 |
| --- | --- |
| `captures` | 采集时间（UTC）、线路、卡片摘要、截图相对路径、处理状态、错误信息和 OCR 耗时。 |
| `freight` | 每个采集编号对应一条识别结果，包含十项货源字段、主引擎 OCR 原文、待核对字段和可选的二次识别意见。 |

货源字段包括运价 `price`、货物重量 `weight`、预估里程 `estimated_distance`、车型要求 `vehicle`、需要车数 `truck_count`、其他信息 `other_info`、卸货地 `unloading_location`、装货地 `loading_location`、货主 `shipper` 和货物 `cargo`。截图未展示或无法确认的值在 SQLite 中为 `NULL`，运价保留页面显示的单位或「电议」。

按 `Ctrl+C` 可中断采集。完整保存的截图组可用 `--process-only` 续处理，同一采集编号重新识别时更新原记录。中断时尚未完成的截图组保留失败状态和已有文件。当前线路、界面布局和 OCR 结果会影响覆盖率，重要字段可用原图复核。

### 常见故障

- ADB 显示 `unauthorized` 时，在手机上确认 USB 调试授权，再运行设备检查命令。
- 程序提示无法确认线路时，检查手机是否回到了原线路的货源列表，再启动下一轮。
- OCR 失败时，可先查看 `captures.error`，再用 `--process-only` 重试。若 Windows OCR 初始化失败，检查是否安装了简体中文 OCR 语言包；二次识别首次运行需联网下载模型。

## 单图 OCR

`x001-ocr` 使用 RapidOCR 的 PP-OCRv5 mobile/OpenVINO CPU 模型识别本地图片，按行输出文字。旧 PP-OCRv6 Medium 仍可通过采集参数 `--ocr paddle` 使用。`x001` 是同一个命令的别名。

```powershell
uv run x001-ocr "D:\images\freight.png"
```

```python
from x001.ocr import OCRRecognizer

recognizer = OCRRecognizer()
texts = recognizer.extract_text("freight.png")
```

## 开发维护

| 模块 | 职责 |
| --- | --- |
| `collect.py` | 线路遍历、折叠展开、详情截图、补拍和采集进程调度。 |
| `export.py` | 按线路筛选已完成记录，校验必填字段并导出可交付 CSV。 |
| `adb.py` | ADB 截图、UiAutomator 点击和滚动。`scroll()` 的 `region` 使用屏幕像素，`up` 指手指上滑。 |
| `vision.py`、`freight.py` | 识别列表卡片、匹配重复访问、提取详情字段和检测浮层遮挡。 |
| `engines.py`、`ocr.py` | OCR 后端和独立图片识别入口。 |
| `storage.py` | SQLite 任务队列、结果写入和中断恢复。 |

采集与 OCR 通过 SQLite 任务状态协调。截图组完整提交后才进入识别队列。UiAutomator 在手机端分段执行曲线手势，列表和详情各自指定滚动区域。开发时可为 `ADBController` 设置固定 `seed`，复现同一手势；测试直线手势时可设置 `humanize=False`。

本地检查无需连接真机。

```powershell
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
```
