# X001 货车帮司机货源采集

X001 从货车帮司机的线路列表读取货源。程序通过 ADB 和 UiAutomator 操作手机界面，保存详情截图，识别货源字段并写入 SQLite。你在手机上选择线路和筛选条件，程序从当前屏幕开始采集一轮。

## 环境准备

- Python 3.14 或更高版本，使用 uv 安装依赖。
- 默认连接小米 MIX 3，ADB 序列号为 `7450df55`，竖屏分辨率为 `1080 × 2340`。采集器的页面布局和手势以这台设备上的货车帮司机为验证基准。
- 手机开启 USB 调试、保持解锁，并授权电脑连接。项目自带 Windows 版 ADB。其他平台需在 `PATH` 中提供 ADB。
- Windows 采集默认优先使用系统的简体中文 OCR 语言包。使用 PaddleOCR 时，Windows 上的 PyTorch 需要 Microsoft Visual C++ 2015–2022 x64 运行库。首次使用 PaddleOCR 时会加载并在本地缺少权重时下载模型。

在项目根目录安装依赖，确认手机在设备列表中显示为 `device`。

```powershell
uv sync
.\src\x001\platform-tools\adb.exe devices -l
```

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
| `--database 路径` | 指定 SQLite 文件，默认是项目目录下的 `data/x001.sqlite3`。 |
| `--max-orders 数量` | 达到指定采集量后结束本轮，适合真机试跑。 |
| `--capacity 数量` | 限制采集中、排队及识别中的截图组总数，默认 3。达到上限后，手机停在线路列表等待。 |
| `--ocr auto\|windows\|paddle` | 默认使用 `auto`，优先使用 Windows 简体中文 OCR，初始化失败时改用 PaddleOCR。 |
| `--process-only` | 只处理当前数据库中待识别、处理失败或上次中断的截图组。 |

### 遍历与结束条件

- 列表中出现折叠提示时，程序先展开，再按新的页面布局重新定位货源。进详情后分屏截图，返回列表后重新识别卡片。
- 程序根据相邻页面重叠的卡片、文字摘要和图像特征减少本轮重复访问。身份有歧义时保留两条。不同运价或装货时间可以分别入库。
- 连续采到 3 条标有「推荐」的货源后，若当前屏没有待采普通货源或折叠入口，程序停止继续采集，回到同一线路顶部并执行下拉刷新。「相似」卡片打断推荐计数。刷新后的列表可供下一轮采集。
- 遇到列表末尾时结束。连续三次翻页没有进展时，控制台会提示未确认到达末尾。达到 `--max-orders` 时直接结束。

## 截图与数据库

默认截图位于 `data/captures/<采集编号>/`。自定义数据库文件使用其同目录下的 `<数据库文件名>-captures/`。每组包含列表原图 `card.png` 和按编号排列的详情图 `detail-01.png` 等。

「货源反馈」浮层遮挡字段时，程序先查找已有截图中的完整值，必要时最多短滑补拍两次。补拍文件名包含 `-supplement`。仍无法确认的字段留空，字段名写入 `review_fields`。

| 表 | 内容 |
| --- | --- |
| `captures` | 采集时间（UTC）、卡片摘要、截图相对路径、处理状态、错误信息和 OCR 耗时。 |
| `freight` | 每个采集编号对应一条识别结果，包含十项货源字段、逐张截图的 OCR 原文和待核对字段。 |

货源字段包括运价 `price`、货物重量 `weight`、预估里程 `estimated_distance`、车型要求 `vehicle`、需要车数 `truck_count`、其他信息 `other_info`、卸货地 `unloading_location`、装货地 `loading_location`、货主 `shipper` 和货物 `cargo`。截图未展示或无法确认的值在 SQLite 中为 `NULL`，运价保留页面显示的单位或「电议」。

按 `Ctrl+C` 可中断采集。完整保存的截图组可用 `--process-only` 续处理，同一采集编号重新识别时更新原记录。中断时尚未完成的截图组保留失败状态和已有文件。当前线路、界面布局和 OCR 结果会影响覆盖率，重要字段可用原图复核。

### 常见故障

- ADB 显示 `unauthorized` 时，在手机上确认 USB 调试授权，再运行设备检查命令。
- 程序提示无法确认线路时，检查手机是否回到了原线路的货源列表，再启动下一轮。
- OCR 失败时，可先查看 `captures.error`，再用 `--process-only` 重试。若 Windows OCR 初始化失败，检查是否安装了简体中文 OCR 语言包。

## 单图 OCR

`x001-ocr` 使用 PaddleOCR 的 PP-OCRv6 Medium 模型识别本地图片，按行输出文字。`x001` 是同一个命令的别名。

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
