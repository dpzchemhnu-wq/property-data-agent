# 物性数据 Agent 接手与排错说明书

## 0. 文档用途

本文档用于把本项目交给新的开发者或其他 AI 继续维护。接手者应先完整阅读本文，再修改代码。本文描述的是当前 GitHub `main` 分支的实际设计，不是早期版本的设想。

当前线上应用：

`https://property-data-agent-pvd5y6dm5iacitfwkhtsxe.streamlit.app/`

当前仓库：

`https://github.com/dpzchemhnu-wq/property-data-agent`

最近一次相关提交：

`36ecdc4 Keep Aspen scan visible and reuse browser window`

项目定位：这是一个化工物性数据查询 Agent，以及一个只读的 Aspen Plus 流股读取桥接。它不是反应器求解器，也不是流程模拟器；LLM 只能把自然语言转换成结构化工具参数，不能生成或修改物性数值。

## 1. 最重要的工程原则

### 1.1 数值必须由工具产生

LLM 只负责意图解析和工具调度。密度、焓、黏度、泡点、表面张力、热导率等数值只能来自：

- `thermo` 本地计算；
- `NIST WebBook` 纯组分参考查询；
- `NIST ThermoML` 混合体系实验数据；
- 可选的 `CoolProp` 纯流体参考计算；
- Aspen Plus 已经收敛的流股结果。

不能让 LLM 直接回答一个看似合理的数值，也不能用默认值掩盖缺失的温度、压力、组分或计算失败。

### 1.2 缺数据必须显式失败

不得把外部数据库没有命中、Aspen 未安装、案例未收敛、参考态不一致或单位未知，包装成“查询成功”。

典型状态码：

| 状态 | 含义 | 正确处理 |
|---|---|---|
| `ok` | 工具完成计算或查询 | 展示结果和来源 |
| `needs_input` | 缺参数、名称无法识别、数量不一致、负配比等 | 明确列出缺少或错误的字段 |
| `out_of_domain` | 温压超出范围，或当前状态为气态而请求液相属性 | 拒绝不适用的液相外推，可保留安全的气相部分结果 |
| `solver_failed` | `thermo`、外部数据库或底层接口失败 | 不发布猜测值，显示失败原因 |

### 1.3 单一事实源和证据链

结果中的每个数值都应尽量带：

- `source_ids`；
- 单位；
- 参考态（尤其是焓）；
- 模型或数据库名称；
- 适用范围或置信度说明。

新的数据源必须作为独立适配器加入，不能把一个来源的数值伪装成另一个来源。

## 2. 目录与职责

| 文件 | 职责 | 修改时注意 |
|---|---|---|
| `app.py` | Streamlit 主页面、自然语言输入、结构化查询、结果详情、数据源选择 | 不在 UI 中直接计算物性，不显示 LLM 猜测数字 |
| `tools.py` | 组分解析、输入校验、配比归一化、模型选择、`thermo` 计算、NIST/ThermoML/CoolProp 参考源 | 所有外部值必须标注来源；不要静默降级 |
| `llm_client.py` | 固定模型服务的 Tool Calling 循环、缺温压拦截、目标物性约束 | 模型只调 `query_properties`，不要添加任意代码执行工具 |
| `local_parser.py` | LLM 不可用时的保守本地解析 | 解析不确定时返回缺参，不要自行填默认工况 |
| `component_aliases.json` | 可编辑的组分名称到 CAS 别名表 | 中文名必须对应已核实 CAS；歧义名称不能强行映射 |
| `simulation_ui.py` | 流程模拟页面、连接器下载、扫描案例、读取流股、Excel 下载 | 未配对时仍保留扫描按钮，点击时检查连接 |
| `aspen_connector.py` | Windows COM/ROT 读取 Aspen，远端 MCP 客户端，单位转换和流股状态门控 | 只读，不启动/修改/保存 BKP |
| `aspen_mcp_server.py` | 本机 HTTP JSON-RPC/MCP 只读桥接服务 | 只开放两个强类型工具 |
| `local_launcher.py` | 本机一键连接器：准备 Python、启动 MCP、启动 Cloudflare Quick Tunnel、打开线上页面 | 随机令牌只用于当前会话，窗口关闭即断开 |
| `simulation.py` | Aspen 流股输入校验、气相/液相/两相分流和物性批处理 | 两相流不要调用单相液体模型 |
| `excel_export.py` | 原始流股表和物性结果表导出 | 保留状态、来源和未计算原因 |
| `test_tools.py` | 67 项边界、解析、物性、Aspen 伪 COM 和 UI 契约测试 | 修改核心逻辑后必须完整运行 |
| `requirements.txt` | Streamlit、thermo、chemicals、SciPy、CoolProp、Windows pywin32 | Aspen Windows 端需要 pywin32 |

## 3. 物性查询完整数据流

### 3.1 自然语言查询

```text
用户输入自然语言
        |
        v
app.py
        |
        +--> llm_client._missing_state_condition
        |       缺温度/压力时直接拦截，不调用模型工具
        |
        +--> 固定模型 Tool Calling
        |       只允许 query_properties
        |
        +--> local_parser（模型不可用时的保守回退）
                |
                v
        tools.query_properties(...)
                |
                +--> 校验组分/配比/T/P/目标物性
                +--> 配比非负且总和大于 0 时归一化
                +--> 组分名称解析为 CAS，保持输入顺序
                +--> 检测 NRTL/UNIQUAC 台账，否则降级 UNIFAC
                +--> thermo 计算
                +--> 查询用户勾选的参考源
                +--> 以 thermo 值为基准逐来源计算偏差
                v
        app.py 只渲染确定结果、来源和折叠详情
```

### 3.2 参数规则

- 温度输入单位最终必须是 K；自然语言中的 `℃`、`°C`、华氏度等由解析层转换。
- 压力最终必须是 Pa；支持常压、atm、bar、kPa、MPa 等明确单位。
- 温度范围：`273.15 K <= T <= 450 K`。
- 压力范围：`1 Pa <= P <= 100000000 Pa`。
- 配比允许总和大于 0 且不等于 1；后端自动归一化。例如 `[5, 1]` 变成 `[0.833333..., 0.166666...]`。
- 负数、全零、缺失、组件数与配比数不一致仍然拦截。
- 用户明确只要密度或泡点时，只返回这些目标物性，不能扩展成全套。
- 没给温度或压力时，必须明确提示缺少哪一项，不能自作主张填 298.15 K 或 101325 Pa。

### 3.3 组分识别

识别顺序大致为：

1. 内置常用中文/英文别名；
2. `component_aliases.json`；
3. `chemicals.identifiers.search_chemical`；
4. 对英文名称尝试 PubChem PUG REST，并验证返回 CAS 能被本地 `chemicals` 识别。

名称解析必须保持用户输入顺序。例如用户输入“水、乙醇”，输出组件顺序必须仍然是水、乙醇。`二甲苯/xylene` 这类可能代表多个异构体的名称应要求用户指定异构体，不能静默替换。

### 3.4 模型选择

`tools.py` 根据二元参数台账动态判断：

- 全部二元对有可追溯 NRTL 参数：优先 NRTL；
- 没有完整 NRTL、但有完整 UNIQUAC：使用 UNIQUAC；
- 都没有覆盖全部二元对：降级 UNIFAC，并显示低置信度和实验校核提醒。

台账记录没有有效 `source_id`、仍是占位引用或缺少参数时，不得视为文献参数。不要把示例台账数字当成真实数据。

### 3.5 相态处理

- 液相：返回请求的液相物性，并标记 `single_liquid_assumed`；当前没有严格 LLE 稳定性校核。
- 气相：不返回不适用的液相表面张力等属性；可返回明确可计算的气相密度、气相黏度、气相热导率或焓，并标记相态。
- 两相或相态无法确认：不要调用单相液体模型；保留原始输入并标注未计算。
- 当前状态高于泡点时返回 `out_of_domain`，提示降低温度或提高压力。

### 3.6 参考源与偏差

页面查询前可选择：

- `NIST WebBook`：主要用于纯组分状态点；
- `NIST ThermoML`：混合体系实验数据，要求组分、组成、温度、压力有直接可比数据点；
- `CoolProp`：开源纯流体参考，不能替代混合物数据库。

`PubChem` 只用于名称/CAS 识别，不作为工艺状态点物性来源。DIPPR、DECHEMA、Aspen Properties 等需要授权的数据没有被伪装接入。

偏差计算公式：

`abs(thermo_value - reference_value) / abs(reference_value) * 100%`

偏差超过 5% 只显示提醒，不替用户裁决哪一个来源正确。单位不同、焓参考态不一致或参考值为零时不计算偏差。

## 4. Aspen Plus 本机连接原理

### 4.1 浏览器为什么不能直接运行 BAT

网页 JavaScript/Streamlit 不能直接执行用户电脑上的 `.bat`，也不能直接访问用户电脑的 Windows COM。用户必须在下载后由浏览器确认运行 BAT，这是浏览器的安全边界，不是代码漏配。

### 4.2 一键连接流程

```text
线上 Streamlit 页面
        |
        | 下载 BAT
        v
用户在 Windows 下载栏确认运行 BAT
        |
        v
Start Aspen Property Agent - One Click.bat
        |
        +--> 从 GitHub 下载 main.zip 到 %LOCALAPPDATA%\\PropertyDataAgent
        +--> 准备 Python 3.12 虚拟环境
        +--> pip install -r requirements.txt
        +--> 启动 local_launcher.py
                |
                +--> 启动本机 127.0.0.1:8765 的 aspen_mcp_server.py
                +--> 生成至少 32 字符随机 Bearer Token
                +--> 启动 cloudflared Quick Tunnel
                +--> 生成 https://*.trycloudflare.com 临时地址
                +--> 通过 URL 参数打开线上页面
                        |
                        v
                simulation_ui.py 将 URL 参数转入 session_state
                并立即清除地址栏中的临时令牌
```

每位用户都有独立的临时隧道和 Streamlit 会话。连接器窗口关闭后，MCP 和隧道都会终止，页面不能继续读取 Aspen。

### 4.3 页面状态

页面的“扫描正在运行的 Aspen 案例”按钮始终显示。点击按钮时：

1. 没有 `aspen_session_mcp_url` 或 `aspen_session_mcp_token`：提示先运行 BAT；
2. 有配对信息：调用 `scan_cases`；
3. 配对成功但返回 `aspen_not_detected`：说明本机没有 Aspen COM 或 Aspen 未安装/未注册；
4. 返回 `no_active_cases`：说明 Aspen COM 已注册，但当前没有可枚举的打开案例；
5. 返回案例列表：只允许选择满足收敛门控的案例。

“没有安装 Aspen”与“网页未配对”是两种不同问题：前者在扫描后报告，后者在扫描前报告。

### 4.4 Aspen 案例门控

`aspen_connector.py` 通过 Windows Running Object Table 枚举 `Apwn.Document`，读取：

- 文档路径和 `.bkp` 文件名；
- `Engine.IsRunning`；
- `\\Data\\Results Summary\\Run-Status\\Output\\PER_ERROR`；
- `\\Data\\Streams` 是否存在及流股数量。

只有以下条件全部满足才可读取：

- Aspen Engine 已停止运行；
- 错误数明确为 0；
- 流股树存在且有流股。

读取流股时会尝试读取温度、压力、摩尔流量、摩尔分率、气相分率，并转换为 K、Pa、kmol/h。未知单位不能猜转换，会把流股标为不完整。

### 4.5 Aspen MCP 接口

`aspen_mcp_server.py` 只提供两个 JSON-RPC 工具：

- `list_aspen_cases`：列出案例状态，不修改 Aspen；
- `read_aspen_streams`：读取已停止且错误数为 0 的案例流股。

服务只监听 `127.0.0.1:8765`，需要 Bearer Token。公网连接依赖 Cloudflare HTTPS 临时隧道；不能把 8765 直接暴露到公网。

## 5. 常见现象与最快定位方法

### 5.1 页面显示“请先下载并运行本机 Aspen 连接器”

这不是 Aspen 未安装提示，而是当前浏览器会话没有读取到 `aspen_mcp_url` 和 `aspen_mcp_token`。检查顺序：

1. 是否真的在浏览器下载栏确认运行了 BAT；
2. BAT 是否自动打开了带临时参数的网址；
3. 连接器黑色窗口是否仍然存在；
4. URL 是否被手动复制成普通主页地址；
5. GitHub/Cloudflare 是否被网络策略阻断。

不要让用户手动填写 URL 或令牌；如果临时令牌泄露，关闭 BAT 并重新运行连接器。

### 5.2 点击扫描后显示“本页面还没有配对本机连接器”

这是同一个未配对状态的主动提示，属于正常保护逻辑。扫描按钮必须保留，不能改成隐藏。正确动作是运行下载的 BAT，等待它打开带配对参数的线上页面，再点击扫描。

### 5.3 点击扫描后显示“未检测到 Aspen Plus”

说明网页配对成功，连接器也能访问，但本机没有可用 Aspen COM。原因可能是：

- Aspen Plus 未安装；
- Aspen 版本没有注册 `Apwn.Document`；
- `pywin32` 安装在错误的 Python 环境；
- Aspen 与连接器不是同一个 Windows 用户会话；
- Aspen 以管理员权限运行而连接器未以相同权限运行。

这不是网页配对 bug。必须在安装并授权 Aspen Plus 的 Windows 电脑上继续测试。

### 5.4 显示“未发现当前用户会话中打开的 Aspen BKP 案例”

说明 Aspen COM 已检测到，但没有枚举到打开的 BKP。确认：

- Aspen Plus 已启动；
- BKP 已在同一个 Windows 用户会话中打开；
- 案例不是只在文件管理器中存在而没有被 Aspen 打开；
- 关闭后重新扫描。

### 5.5 有案例但不能选择

这是故意的安全门控。案例可能仍在计算、错误数不为 0、错误数无法读取或没有结果流股。不能为了“让按钮可用”而放宽门控，否则会读取不完整结果。

### 5.6 自然语言查询没有结果

按顺序检查：

1. 是否给出了温度和压力；
2. 是否给出了组分和配比；
3. 是否点名了目标物性；
4. 组分名称是否存在歧义；
5. `chemicals`/`thermo` 是否安装；
6. 模型服务是否返回了合法 Tool Calling；
7. 本地解析是否能识别单位和比例。

模型服务不可用时，只有明确可解析的输入才会走 `local_parser`；不能让本地解析器猜测缺失工况。

### 5.7 NIST/ThermoML 未覆盖

这不一定是连接错误。ThermoML 必须有与当前组分、摩尔分数、温度、压力直接匹配的公开实验点，代码不做任意插值或外推。详情中应区分：

- 未覆盖；
- 找到候选文献但没有当前状态点；
- 接口暂不可用；
- 请求超时。

不能把“未命中数据点”改写成“数据库连接失败”，也不能用 thermo 值冒充 NIST 值。

### 5.8 页面仍显示旧版本

检查：

1. GitHub `main` 是否已出现最新提交；
2. Streamlit Cloud 是否完成重新部署；
3. 浏览器是否执行 Ctrl+F5；
4. 线上页面是否仍缓存旧会话。

当前线上地址返回 HTTP 200 只代表服务可访问，不代表浏览器已经加载最新 Python 代码。

## 6. 本地验证命令

不要使用系统中旧的 Python 3.7。当前项目验证环境是 Python 3.12 虚拟环境：

```powershell
cd "D:\ChatGPT\workspace\2026-10-30\agent-v3-ai-claude-gpt-cursor\work\github-deploy\property-agent"

# 语法检查
& "D:\Process Analysis\property-agent\.venv\Scripts\python.exe" -m py_compile `
  app.py tools.py llm_client.py local_parser.py simulation.py `
  simulation_ui.py aspen_connector.py aspen_mcp_server.py local_launcher.py

# 完整测试
& "D:\Process Analysis\property-agent\.venv\Scripts\python.exe" -m unittest test_tools.py -v

# 直接启动本地页面
& "D:\Process Analysis\property-agent\.venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501
```

当前核心测试包括：

- 温度/压力缺失拦截；
- 摄氏度、华氏度、压力单位转换；
- 正配比归一化、负数/全零拒绝；
- 中文别名和输入顺序；
- 目标物性不扩展；
- NIST WebBook 返回解析和连接失败区分；
- ThermoML 精确组成/状态点匹配；
- CoolProp 纯流体参考源；
- 气相和两相流处理；
- Aspen 伪 COM 树、单位转换、错误数门控；
- MCP 工具白名单和令牌校验；
- 多次查询页面状态。

## 7. 修改代码时的禁止事项

不要做以下修改：

1. 在 `tools.py` 中写死某一套组分体系；
2. 用经验常数或 LLM 文本填补 `thermo` 失败；
3. 缺少温度/压力时自动填默认值；
4. 把所有来源直接拼成一个“平均值”；
5. 将 NIST 未覆盖显示成已覆盖；
6. 将 Aspen 未安装显示成网页连接失败，或反过来；
7. 让网页直接执行 BAT 或把 MCP 监听改成公网 `0.0.0.0`；
8. 通过 URL、日志、异常文本输出 API Key 或临时令牌；
9. 为了测试方便放宽“案例已停止且错误数为 0”的门控；
10. 修改后不运行完整测试就推送到 GitHub。

## 8. 安全与凭据

- 模型地址和模型名在代码中固定为当前项目配置；API Key 只能从 Streamlit secrets 或环境变量读取。
- 不要把 API Key 写入 GitHub、Markdown、BAT 或测试输出。
- Aspen 临时 URL 和 Token 是当前会话的临时访问凭据，不要复制给他人。
- MCP 只开放只读工具，不接受任意 COM 路径或模型生成代码。
- Cloudflare Quick Tunnel 地址会变化；每次连接器启动都应生成新的临时令牌和页面参数。

## 9. 接手 AI 的推荐工作顺序

当用户报告一个错误时，接手 AI 应按下面顺序工作：

1. 先确认用户是在“物性数据查询”还是“流程模拟调用”；
2. 记录错误的完整文字、当前页面状态和是否点击过 BAT；
3. 根据本文第 5 节把现象归类为未配对、未安装 Aspen、无案例、案例未收敛、物性输入问题、外部数据库未覆盖或部署缓存；
4. 先读取对应模块源码和测试，不要立即重写整个项目；
5. 用 Python 3.12 运行最小复现，再运行完整测试；
6. 修复后补一个针对该错误的回归测试；
7. 检查 `git diff --check`、语法和页面冒烟测试；
8. 提交并推送 GitHub，说明提交号、验证结果和仍然存在的真实环境限制。

如果问题涉及真实 Aspen COM，而当前机器没有 Aspen，应明确说明“未完成真实 COM 联调”，不能声称已经验证成功。

## 10. 当前已知限制

1. 开发机没有 Aspen Plus，因此 COM Running Object Table、特定 Aspen 版本的 Tree 路径和真实 BKP 读取仍需在安装 Aspen 的 Windows 电脑上验收。
2. 浏览器无法无提示执行 BAT；用户第一次必须确认下载文件。
3. Cloudflare Quick Tunnel 是临时隧道，连接器窗口关闭后网页不能继续访问本机 Aspen。
4. ThermoML 的公开覆盖和实验状态点有限；未命中时不能通过猜测补齐。
5. DIPPR、DECHEMA、Aspen Properties 等授权数据源未接入。
6. `thermo` 的焓基准需要与下游流程模拟软件对齐，不能直接把不同参考态的焓做偏差比较。

这份说明书的原则是：先判断边界，再定位代码；先保留真实失败，再做可追溯修复。任何“看起来有结果但来源不清楚”的修复都不符合本项目要求。
