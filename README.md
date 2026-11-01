# 物性数据 Agent V3

## 文件

- `app.py`：Streamlit 对话界面和固定模型服务的受限工具调用循环。
- `tools.py`：通用组分识别、输入校验、参数选择、物性计算、参考源查询和状态码处理。
- `aspen_connector.py`：Windows Aspen COM 活动案例读取器，以及云端到本机桥接的客户端。
- `aspen_mcp_server.py`：在 Aspen Windows 主机运行的 MCP 只读工具服务。
- `simulation.py` / `simulation_ui.py`：流程模拟器输入校验、流股相态分流和查询页面。
- `excel_export.py`：原始流股和 Agent 物性结果的双工作表 Excel 导出。
- `requirements.txt`：运行依赖。
- `.env.example`：部署主机的密钥变量模板；固定模型服务不可在网页修改。

## 启动

需要 Python 3.10 或更高版本。此机器已将 Python 3.12.10 安装在 `D:\Process Analysis\python312`，并将应用运行环境、依赖与缓存放在 `D:\Process Analysis` 下。

```powershell
cd outputs
python -m venv .venv
.
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

模型接口和模型名由后端固定；API Key 从本机 Streamlit secrets 或进程环境读取。密钥不要写入源码、`.env.example`、公开仓库或共享给其他电脑的项目包。部署到另一台机器时，应在那台服务器单独设置密钥。

如项目使用非英文组分别名，优先在 `component_aliases.json` 中扩充名称到 CAS RN 的映射；其余名称仍会尝试通过 `chemicals` 索引通用识别。结构化表单支持英文逗号、中文逗号、顿号、分号和换行分隔。

## 参数台账

将台账存为 UTF-8 JSON，并设置 `BINARY_PARAMETER_LEDGER`。CAS 是稳定键，方向参数随 `CAS1`、`CAS2` 顺序定义。每条二元对记录都必须带可追溯的 `source_id`，否则不会被视为已检测到的文献参数。NRTL 参数是无量纲 `tau = tau_a + tau_b/T`，`alpha_ij` 为非随机参数；UNIQUAC 参数 `u_ij_J_mol`、`u_ji_J_mol` 单位为 J/mol，纯组分结构参数 `r`、`q` 放在 `components` 中。

```json
{
  "components": {
    "64-17-5": {"r": 2.1055, "q": 1.972},
    "67-56-1": {"r": 1.4311, "q": 1.432}
  },
  "pairs": [
    {
      "model": "NRTL",
      "CAS1": "64-17-5",
      "CAS2": "67-56-1",
      "tau_a_ij": 0.0,
      "tau_a_ji": 0.0,
      "tau_b_ij": 0.0,
      "tau_b_ji": 0.0,
      "alpha_ij": 0.3,
      "source_id": "literature:replace-with-verified-citation"
    },
    {
      "model": "UNIQUAC",
      "CAS1": "64-17-5",
      "CAS2": "67-56-1",
      "u_ij_J_mol": 0.0,
      "u_ji_J_mol": 0.0,
      "source_id": "literature:replace-with-verified-citation"
    }
  ]
}
```

上面数字仅用于说明 JSON 字段结构，不是推荐或有效的文献参数。只有当台账完整覆盖混合物的全部二元对时，才选用 NRTL 或 UNIQUAC；否则尝试 UNIFAC。上线前应由团队用经过审核的文献数据替换示例数值，并为参数记录增加出处、温度范围、拟合目标与版本信息。

## 外部参考数据边界

纯组分通过 NIST WebBook Fluid CGI 查询给定温度/压力下的状态点。混合体系直接连接 NIST/TRC 官方 ThermoML：搜索端点 `https://trc.nist.gov/ThermoML-API/objects` 用于检索候选文献元数据；随后按 DOI 从 `https://trc.nist.gov/ThermoML/{DOI}.json` 读取实际实验数据点，并仅在组分和液相摩尔分数匹配、温度及压力位于明确的实验取整容差内时比较。该 JSON 路径与 NIST 官方站点提供的搜索说明及 DOI 对象链接一致。官方归档说明：完整数据集也可从 [NIST ThermoML Data Archive](https://data.nist.gov/od/id/mds2-2422) 下载约 189 MB 的 `.tgz`。当前集成采用按需下载单篇 JSON，避免每台安装都必须先拉取整个归档。

`thermo` 物性计算不需要模型密钥；NIST 参考数据也不要求单独密钥，但查询时需要互联网连接。自然语言输入默认交给固定模型服务转换为工具参数（摄氏/华氏温度、压力单位和配比均转换为 K、Pa 和摩尔分数）；模型不可用时才尝试保守的本地解析。外部源不可达或超时会明确报告，不以模型值替代参考数据。

ThermoML Search API 本身仅供检索元数据/摘要，不包含数值数据点；因此代码不能只依赖搜索 API 就声称已做数值比较。当前会扫描排序前 5 页（最多 100 条候选）并按 DOI 读取最多 12 篇相关完整 JSON；受官方覆盖范围（公开站点标注截至 2019 年）、文献实验组成/温度/压力离散点及检索排序影响，未命中时会明确说明“找到候选文献但没有当前状态点”，不会静默跳过或插值/外推。常压的 100 kPa、101.3 kPa 和 1 atm 允许小范围取整差；温度和摩尔分数仍要求直接实验点匹配。泡点暂不从文献数据猜算；焓只有参考态明确一致才计算偏差；来源不确定度按 ThermoML 扩展不确定度显示。WebBook 和 ThermoML 各自单独成列，相对偏差按来源分别计算。

## 重要工程边界

- 输入温度范围为 273.15–450 K，压力范围为 1–100,000,000 Pa；范围外返回 `out_of_domain`。
- 组分与摩尔分数按输入顺序传递；输入值只需非负且总和大于 0，后端会自动按总和归一化。负数、全零、缺失或数量不匹配仍会拦截。
- 本地规则解析是默认流程；无法识别的完整自然语言请求会交给固定语言模型辅助解析。界面不提供模型/接口选择；密钥由运行主机安全配置。界面仅将后端返回的数值作为物性结果展示。
- `thermo` 给出的焓使用其默认基准，必须与下游模型对齐；相态枚举为 `single_liquid_assumed`，未做严格 LLE 稳定性校核。
- 生产使用前应以目标组分、目标相态和目标压力范围进行回归验证，并记录 `thermo` 数据库/版本和文献参数出处。

## Aspen Plus 连接

侧栏入口名为“流程模拟调用”。真实 Aspen 读取采用论文所述的 MCP 模式：Windows 主机运行 `aspen_mcp_server.py`，只公开 `list_aspen_cases` 和 `read_aspen_streams` 两个强类型只读工具；工具内部再用 COM Running Object Table 枚举当前用户会话中已打开的 Aspen 文档，筛选 `.bkp`，并读取 Aspen Tree 中的运行状态、错误数、流股温度/压力/摩尔流量/摩尔分率和气相分率。模型和网页不能提交任意 COM 路径或代码。仅当 Aspen 已停止计算、错误数明确为 0 且流股树存在时，案例才可选。

### 在 Aspen 主机本机运行

1. Windows 安装并授权 Aspen Plus；由同一个 Windows 用户打开已运行成功的 `.bkp` 案例。
2. 在该电脑安装 Python 3.12 和项目依赖：`pip install -r requirements.txt`。Windows 依赖会安装 `pywin32`。
3. 从同一桌面用户会话运行 Streamlit 应用，打开“流程模拟调用”并点击“扫描正在运行的 Aspen 案例”。

扫描时必须能从 Aspen 暴露 `Engine.IsRunning`、`\Data\Results Summary\Run-Status\Output\PER_ERROR` 和结果树。错误数节点路径可以用 `ASPEN_ERROR_COUNT_PATH` 覆盖；单位从 Aspen `\Unit Table` 解析，未知单位不会猜测转换，相关流股会标为不完整。

### Render 网页访问 Aspen 主机

Render 容器运行于 Linux，不能直接读取用户 Windows 桌面、COM ROT 或本地 `127.0.0.1`。为让 Render 网页访问 Aspen 主机，必须在 Aspen 电脑启动只读桥接服务，再使用 HTTPS 隧道将其转发到 Render。桥接接口只开放案例清单和流股读取，并要求至少 32 字符的 Bearer 令牌。

在 Aspen Windows 主机 PowerShell 中设置同一 MCP 令牌并启动：

```powershell
$env:ASPEN_MCP_TOKEN = "使用密码管理器生成的随机长令牌"
python aspen_mcp_server.py
```

MCP 服务默认只监听 `127.0.0.1:8765`。使用 Cloudflare Tunnel 或组织批准的 HTTPS 隧道将其映射为稳定 HTTPS 地址；不要直接把 8765 端口暴露到公网。在 Render 环境变量中私密设置 `ASPEN_MCP_URL` 为该 HTTPS 地址，`ASPEN_MCP_TOKEN` 填同一令牌。`render.yaml` 已声明这两个环境变量。MCP 进程必须保持在 Aspen 登录用户的桌面会话中运行，Aspen 关闭或用户注销后查询将不可用。

仓库提供 `Start Aspen MCP.ps1`：在 Aspen Windows 主机上运行会安装项目依赖（包括 Windows 的 `pywin32`）、生成本次运行的随机令牌并启动 MCP 只读服务。首次运行的令牌只显示在本机终端，需通过 Render 私密环境变量设置 `ASPEN_MCP_TOKEN`。脚本默认只监听本机回环地址，不会自行暴露公网端口。

Render 还需要一条从互联网到 Aspen 主机的 HTTPS 通道。临时演示可在 Aspen 主机另开 PowerShell 窗口运行 `cloudflared tunnel --url http://127.0.0.1:8765`，将其生成的 HTTPS 地址填入 Render 的 `ASPEN_MCP_URL`。Quick Tunnel 的地址会在隧道重启后变化；长期演示应由项目所有者在 Cloudflare 配置具名 Tunnel/固定域名，或使用学校批准的 VPN/反向代理。不要将 MCP 改成 `0.0.0.0` 监听或直接开放 8765 入站端口。

验证连接时先在 Aspen 主机浏览器访问 `http://127.0.0.1:8765/health`，应返回服务健康状态；再到网页点击“扫描正在运行的 Aspen 案例”。健康检查仅验证桥接进程，不代表 Aspen 案例可读。必须由同一个 Windows 用户会话打开 Aspen，且案例已停止计算、错误数为 0，才能读取。桥接本身不会启动、修改或保存 BKP 文件。

### Aspen 案例读取

“流程模拟调用”页面只连接真实 Aspen Plus，不提供文件上传或模拟数据入口。点击“扫描正在运行的 Aspen 案例”后，页面枚举当前 Aspen 用户会话中的 BKP，只有已停止计算、错误数为 0 且存在结果流股的案例才可选择。读取后继续按流股相态调用 Agent 物性计算，并可下载流股与物性 Excel。

Aspen `.bkp` 是专有结果归档，网页不会尝试在云端猜测或解码它。对方只拿到网址时，还需要在其安装 Aspen Plus 的 Windows 电脑上启动 `aspen_mcp_server.py`，通过 HTTPS 隧道把 MCP 服务发布给网页，并设置 `ASPEN_MCP_URL` 与 `ASPEN_MCP_TOKEN`。浏览器无法绕过 MCP 服务直接访问另一台电脑的 Windows COM。

### 线上网页 + 本机 Aspen 一键配对

课程线上网页为 `https://property-data-agent-pvd5y6dm5iacitfwkhtsxe.streamlit.app/`。未配对时，网页显示“下载并连接本机 Aspen（Windows）”按钮，下载由 GitHub 部署版本提供的一键 BAT。用户在浏览器下载栏点击打开后，BAT 自动下载当前项目、准备 Python 运行环境与 Cloudflare Tunnel，启动只读 Aspen MCP 服务，为本机创建临时 HTTPS 隧道，再打开线上网页并将这位用户的临时 MCP 地址/随机令牌绑定到当前 Streamlit 会话。网页进入“流程模拟调用”后点击扫描，即扫描该用户本机 Aspen；不同用户使用不同浏览器会话和隧道，可同时连接各自 Aspen。用户不需要填写 URL、令牌、配对码或 Render 环境变量。

首次使用需要联网下载项目、Python 依赖以及 Cloudflare Tunnel 程序，可能耗时数分钟；之后重复启动会复用本地文件。电脑必须已安装并授权 Aspen Plus。启动器窗口必须保持打开；关闭窗口会停止隧道与 MCP，本次线上连接随即失效。Quick Tunnel 地址每次启动都可能改变，因此每次都从 BAT 打开新的配对网页，不要复用旧标签。

浏览器出于安全隔离，不能由网页按钮直接执行 BAT；用户仍需确认打开下载文件一次。随机令牌只为本次启动生成，线上页面读取后会立即从地址栏移除。不要复制/分享启动器刚打开时的完整带参数地址；它在连接器运行期间相当于本机 Aspen 的临时只读访问凭证。MCP 仅开放枚举案例和读取流股两个只读工具，仍应只在可信设备上使用。

当前开发机没有 Aspen Plus、`Apwn.Document` 注册或真实 BKP，因此 COM 对象模型、状态路径和特定 Aspen 版本的流股节点尚不能在此机完成集成验证。代码采用严格失败关闭；真实安装后的第一轮部署仍需在目标 Aspen 版本上核验节点路径和单位表。自动化测试使用伪造的 COM 对象树验证转换和门控，不代表 Aspen 联调通过。

测试：在项目 Python 环境中运行 `python -m unittest test_tools.py -v`。

## 调试与多人运行

- 模型服务请求仅用于自然语言转工具参数；401、503 等连接错误折叠显示，详情中不会回显密钥。模型地址和名称在后端固定。
- 工具计算成功后直接返回卡片，不再额外请求 LLM 做二次总结，避免模型的第二次 HTTP 错误遮住已经计算出的结果。
- 自然语言默认由固定模型转换为工具参数，中文组分来自包含 200+ 别名的可编辑名录，英文名优先用 `chemicals` 本地索引，未命中时再查 PubChem 并验证 CAS。明确比例按总份数归一化（如 `甲醇与甲苯摩尔比 5/1` 转成 `[0.833333, 0.166667]`）；明确质量比不会伪装成摩尔比。对于名称有歧义或不能核实唯一 CAS 的物质，会要求用户明确名称，不猜测替代组分。仅返回用户明确点名或结构化勾选的目标物性。
- 单机运行绑定地址默认为 `127.0.0.1`。在团队 LAN 共享时，需由管理员按本机网络策略开放 TCP 8501，且仅在可信网络使用。
- 其他 Windows 电脑可安装 Python 3.12 后，在项目目录创建虚拟环境并执行 `pip install -r requirements.txt`。若团队电脑装有 Docker，运行 `docker compose up --build`，通过 `http://localhost:8501` 使用；同一 LAN 的其他电脑使用主机内网 IP 和 `:8501`。API Key 不要提交进共享代码仓库。
- 公网部署模板为 `render.yaml`，容器会读取平台分配的 `PORT`。将代码放入私有或受控 GitHub 仓库后，在 Render 创建 Web Service 并选择该 Blueprint；在平台的环境变量面板私密填写 `API_KEY`。部署完成后，平台会分配可供电脑和手机访问的 HTTPS 地址。公开服务可能消耗模型账户额度，正式分享前应设置访问控制或预算限制。
- 本机若要临时给同一可信 LAN 的同学演示，可运行 `D:\Process Analysis\Start Property Agent LAN.bat`，然后将本机内网 IP 和 `:8501` 发给对方；不要在不可信公共网络直接暴露该端口。

## 本机网页应用

在此机器上双击 `D:\Process Analysis\Start Property Agent.bat` 即可启动，浏览器地址为 `http://127.0.0.1:8501`。自然语言输入由后端固定的服务和模型转换，网页不显示模型选择；模型不可用时才回退到本地解析。API Key 仅保存在本机 `.streamlit\secrets.toml`（已排除在项目文件之外）或服务进程环境中。手动结构化查询收在折叠区；结果表将单位放在物性表头，来源对照分别折叠显示。

本机 Python、应用虚拟环境、安装器、临时文件和 pip 缓存分别位于 `D:\Process Analysis\python312`、`D:\Process Analysis\property-agent\.venv`、`D:\Process Analysis\installers`、`D:\Process Analysis\temp` 和 `D:\Process Analysis\package-cache`。
