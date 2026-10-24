# 0.3.1 稳定性与环境兼容改进

本轮不改变 5 秒自动审批功能，不进行长期运行压测。版本升级后 Broker、MCP 和插件须来自同一完整发行包。

## 等待与恢复

正数 `wait_seconds` 支持短轮询，上限 900 秒；0 表示立即读取。待确认是否过期以冻结的单调有效期限为准，重复或并发轮询不会提前耗尽有效时间。收到 `polling.next_arguments` 后继续原请求，不要重新提交动作。

提交前保存最小恢复记录，用随机恢复凭据定位原请求；它不包含命令正文或输出。恢复只查询已有请求，回执丢失不等于未执行。窗口重启会丢失内存请求，这时必须核对实际结果，不能自动重放命令。Windows 下恢复凭据使用当前用户 DPAPI 保护；聊天线索仅用于本地分区，不是授权证明。没有稳定分区时不得自动复用其他连接器的记录。

## 项目环境与命令预检

`preflight_command` 只检查路径、解释器、命令行长度、环境变量名称、编码和直接文件指纹，不创建授权卡片也不执行程序。真正提交时仍会再次校验。

`request_command_execution` 支持：

- `environment`：字符串键值覆盖，如 `{"VIRTUAL_ENV":"D:\\project\\.venv"}`。未覆盖变量继承窗口环境；不会执行变量展开或激活脚本。解释器最好直接使用虚拟环境中的绝对 EXE 路径。相对 PATH 项按请求工作目录解析；完整覆盖值可在本机命令详情核对，预检和脱敏诊断只返回变量名。
- `output_encoding`：默认 `auto`，也可指定 UTF-8、UTF-16、GBK/CP936 等受支持编码。编码错误只影响输出呈现，不改变命令状态。
- `response_files`：明确列出工具支持的参数文件，且参数数组必须包含对应 `@path`。文件会被哈希并在启动前复验；文件格式和编码由目标工具规定，金手指不替工具展开文件内容。

命令长度按 Windows 实际引号规则及 UTF-16 单元校验，超过 32767（含结束符）在提交阶段拒绝。输入参数始终作为数组处理。

## 可复用模板

`scripts/command_templates.py` 只生成经过预检的 JSON，不运行命令。支持 `python`、`powershell`、`node`、`npm`。例如：

```powershell
python scripts/command_templates.py python --executable D:\Project\.venv\Scripts\python.exe --script D:\Project\check.py --directory D:\Project -- --verbose
python scripts/command_templates.py powershell --executable C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe --script D:\Project\check.ps1 --directory D:\Project
python scripts/command_templates.py npm --executable C:\Tools\node.exe --script C:\Tools\node_modules\npm\bin\npm-cli.js --directory D:\Project -- run test
```

npm 模板直接调用 Node 与 `npm-cli.js`；不会把 `npm.cmd` 或任意 shell 字符串当作 EXE。交互式 stdin 不受支持，预检会明确报告该限制。填写精确动作、风险、恢复方式等字段后才能提交授权请求。

## 输出与进度

日志在内存中有界保留头尾，末尾错误不会因为前段输出过多而全部丢失。`read_command_output` 需要原始请求 ID 与 claim，按字节游标分页读取；超过保留量的中间段会明确标记缺口。它不保证保存无限完整日志；需要完整日志时，仍由命令显式写入用户指定的文件。

单页装不下一个完整字符时，会返回 `output_page_limit_too_small` 和建议的 `minimum_limit`，用更大页从原位置重试。运行中只写出半个字符时，可暂时返回空文本和不变的游标，等待后续字节。

运行中的摘要和最近输出可以读取。传入 MCP `progressToken` 的客户端可收到节流进度通知；进度表示实际阶段或经过时间，不伪造完成百分比。通知在工具调用结束后停止。

## 安装修复

稳定安装根将保存独立恢复工具，管理入口不再依赖活动版本中的 Setup。完整发行包可作为修复来源；修复和恢复必须通过完整性校验、运行中检查以及原子切换。

`repair --from <完整发行目录或ZIP> --root <安装根>` 用于修复相同发行版本的损坏文件。`recover --version <versions下完整目录ID> --root <安装根>` 用于恢复损坏的活动指针。已有请求不会跨窗口重启恢复，也不会自动重放。

独立工具位于 `<安装根>\tools\recovery\bin\GoldenFingerSetup.exe`。先用 `versions --root <安装根>` 查看完整版本目录 ID。若独立工具本身损坏，使用外部完整发行包的 Setup 执行 `repair --from <完整发行包> --recovery-tools --root <安装根>`；修复前的副本保留在安装根的 `quarantine` 中。

启动检查等待精确版本的 Broker 就绪，提前退出或构建不匹配会明确报错。执行命令期间不自动重启窗口。

## 验证范围

本轮通过定向并发、编码、长度、断线、分页及安装故障回归验证；长时间压测、真实多客户端应用互操作、不同 Windows/DPI/组织策略环境留待后续机器验收。当前改进不等于已经在所有 MCP 客户端验证兼容。

进度令牌、递增值和完成后停止的约束参考 [MCP 2025-06-18 进度规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/progress)；命令长度包含结束符的上限参考 [Windows CreateProcessW 文档](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)。
