# 微观形貌 `.xls` 的 Windows Excel 只读验证器

此工具使用目标环境中的 Microsoft Excel COM 打开微观形貌原始记录，验证
LibreOffice 生成的 legacy `.xls` 在真实 Excel 中是否仍保持正确图片几何。
它不会保存、另存、导出或打印工作簿。

## 运行条件

- Windows 10/11；
- 已安装桌面版 Microsoft Excel；
- Windows PowerShell 5.1；
- 运行账号对工作簿具有读取权限。

验证器会启动一个独立、隐藏的 Excel 实例，并以 `ReadOnly=true`、
`DisplayAlerts=false` 打开工作簿。正常或异常退出都在 `finally` 中执行
`Workbook.Close(false)` 和 `Excel.Quit()`。如果 Excel 因名称冲突弹窗等原因
超过默认 45 秒仍未返回，外层启动器只终止本次 Worker 及其记录的独立 Excel
进程，并返回 `excel_open_timeout`。

## 基本用法

```powershell
cd .\scripts\windows\microscopy-workbook-verifier
.\Verify-MicroscopyWorkbook.ps1 `
  -WorkbookPath 'C:\records\260061860-图片-纤维微观形貌原始记录.xls'
```

标准输出只有一个 JSON 对象；`ok=true` 表示全部内建检查通过。进程退出码：

- `0`：全部检查通过；
- `1`：成功完成验证，但存在不符合项，或执行失败；
- `2`：Excel 打开/验证超时，通常意味着阻塞弹窗或损坏文件。

内建检查包括：

- Excel 能在时限内无阻塞弹窗地打开工作簿；
- 工作簿确实以只读方式打开，且存在“微观形貌”工作表；
- `Print_Area` 名称恰好一个，且打印区域默认为 `$A$1:$L$37`（期望清单可覆盖）；
- 至少存在一张图片；
- 列举图片的 `Left/Top/Width/Height`、宽高比和比例锁状态；
- 每张图片均位于 `A4:L32` 边界内；
- 图片彼此不重叠；
- 验证前后文件长度、修改时间和 SHA-256 完全一致。

图片按 `Top → Left → Name` 排序并使用从 1 开始的 `index`，便于多图结果稳定比对。

## 带期望清单验证

```powershell
.\Verify-MicroscopyWorkbook.ps1 `
  -WorkbookPath 'C:\records\multi-image.xls' `
  -ExpectationPath '.\examples\expectations.example.json' `
  -TimeoutSeconds 60
```

期望清单可以指定图片数，以及每张图片的预期宽高比和可选几何位置。未填写的
字段不会参与比较：

```json
{
  "worksheet_name": "微观形貌",
  "canvas_address": "A4:L32",
  "image_count": 2,
  "position_tolerance_points": 2.0,
  "size_tolerance_points": 2.0,
  "aspect_ratio_tolerance": 0.02,
  "images": [
    { "index": 1, "aspect_ratio": 1.333333 },
    { "index": 2, "aspect_ratio": 0.75 }
  ]
}
```

每个 `images[]` 项还可选填 `left`、`top`、`width`、`height`。这些数值与 Excel
COM 一样使用 point（磅）。

## 纯 PowerShell 自检

自检不需要打开 Excel，包含几何、边界、重叠、宽高比容差和 Print Area 名称
规范化的单元式测试，以及确保主脚本没有保存/打印调用、始终在 `finally` 退出
Excel 的静态检查：

```powershell
.\tests\Run-SelfTests.ps1
```

发布前仍应再用真实生成的单图和多图工作簿执行一次 COM 验证；纯脚本自检不能
代替真实 Excel 对 BIFF `.xls` 锚点的解释。
