# LLM API Benchmark Tool(myoptool)

本地网页版 LLM 工具链(雍宁工具链),一个进程四个站点:**压测工作台**(对话/文生图/文生视频压测,多任务并行,自动生成 Excel 报告含 AI 分析页)、**ModelUse 模型工作台**(对话 + API 网关,独立页 `/modeluse`)、**模型部署站**(SSH 主机 / Docker·K8s 容器运维 + 预设命令 + 任务编排,独立页 `/modelstart`)、**GPU 监控**;内置多账户体系(总管理员/管理员/子账号/预览用户,按模块授权)。前端基于 Vue 3 + Element Plus + Apache ECharts(零构建自托管,离线可用),深/浅双主题,字号调节,压测等待时可玩贪吃蛇。

仓库:<https://github.com/marxyong2-cloud/myoptool>

## 功能一览

- **API 配置**:填地址 + Key(可留空),点「自动检测」→ 自动识别协议(OpenAI/Anthropic/Ollama)、推理框架(vLLM/SGLang/TGI 等)、拉取模型列表、自动探测上下文上限;**上下文探测可随时停止**(自动检测后自动跟随执行的探测,探测中再点一次「探测最大输入/输出 tokens」按钮即中止,后端立即停止后续梯度请求);已保存 API 下拉快速切换(切换后自动重新检测)并可删除(「删」按钮);对话面板模型下拉(▾)中每个模型可单独从历史删除;**配置自动持久化(本机浏览器),刷新页面后 API/模型/上限全部自动回填,无需重新检测**
- **服务信息(全量)**:检测/切换 API 后自动探测全部可用端点 — /health、/version、/server_info(vLLM 服务配置全量键值)、/get_server_info(SGLang)、/v1/models 模型列表、/metrics 运行指标(vLLM/SGLang 自适应:KV Cache 使用率、前缀缓存命中率、运行/排队、生成吞吐);直连被网关拦截时,若已配置该主机 SSH 直连监控,自动经 SSH 通道在主机内部获取(含监听端口发现,可找到被隐藏的 worker metrics)
- **KV Cache 效率测试**:左栏底部独立板块,自动显示服务 prefix_caching 配置状态;四种通用场景(参照 vLLM/SGLang prefix caching 基准):冷热对比(同 prompt 两次)、多轮增量(上下文逐轮增长验证只 prefill 新增部分)、共享前缀并发(同前缀不同后缀 K 路并发命中)、缓存逐出(扰动后验证容量/LRU);支持多输入长度(256~8192 tok)依次测试,自动给出结论与 vLLM /metrics 命中计数佐证;「一键全套诊断」依次执行四种场景并汇总
- **多模态**:对话(chat)/ 文生图(image)/ 文生视频(video)三种任务类型,检测时自动探测能力并可点切换
- **主页面布局**:左栏加宽并分为三个模块卡片 —「🔌 API 配置」(地址/密钥/协议/模型/服务实时指标)、「🧪 测试策略」(每行直接可编辑的策略表 + 创建压测任务 + 报告路径)、「⚡ KV Cache 效率测试」(多选输入长度依次测试)
- **策略压测**:默认 16 条策略(并发 4/16/32/64 × 4 种输入输出组合),每行直接可编辑,可勾选/增删;「智能添加策略」按探测上限自动调整(80% 安全预算);「📈 并发扫描」固定 ISL/OSL 一键生成 1~64 七档并发策略(用于画延迟-吞吐/Goodput 曲线,单点对比易误判);每策略可选**缓存模式**(默认跟随全局随机/固定;冷=每请求注入唯一前缀,前缀缓存必未命中,测纯引擎 prefill 能力;热=固定同文,首请求后全命中,测缓存收益上限)与**预热请求数**(前 N 个仅执行不计入统计,剔除冷启动);单策略请求数上限 2000(P99 有统计意义建议每组 ≥200 个请求)
- **对比受控项(SLO/采样/硬件)**:策略卡下方可设 **SLO 阈值**(TTFT≤X ms 且 TPOT≤Y ms,0=不启用),设置后以 **Goodput(满足 SLO 的有效请求速率)+ SLO 达标率**为最终对比口径(失败请求计入分母,消除幸存者偏差);**采样参数**(temperature/seed)固定后不同轮次的解码路径才可复现;**GPU/硬件环境**说明与「自动检测」抓到的引擎版本/部署参数(TP/dtype/上下文上限等)一起写入报告头环境信息块,跨环境对比可溯源
- **多任务并行**:可创建多个任务同时压测不同 API;任务待启动/暂停/继续/改名/删除,暂停时可编辑剩余策略;点击任务列表切换查看,**结果汇总与日志跟随所选任务**(历史任务自动回填全部策略结果与摘要日志)
- **实时面板(策略时间线)**:总进度条 + 实时 req/s / tok/s / 成功率 / 错误率;「策略执行状态」以纵向时间线跟随各策略自身进度 — 已执行策略在上方折叠淡化(✓ 成功率 · 延迟P50;含失败显示 ⚠ 失败数),**当前策略展开:自己的进度条 + 下方状态行(成功/失败/并发/输出上限/语言)**,未执行策略在下方折叠淡化(○ 并发数·请求数),自动滚动跟随当前策略,悬停查看完整配置
- **失败原因可诊断**:实时日志对每个失败请求给出「分类原因 + HTTP 状态码 + 完整原始报错」(如 `#12 FAIL [HTTP 404] 接口不存在(404) — 路径或模型名写错` + 原始报错行);每个策略完成后输出失败原因汇总(服务端按结果分组,回看任务同样可见,如 `连接被拒 × 4`);汇总表与 Excel 均含「失败原因」列,并按标准分桶给出 超时%/限流429%/服务端5xx%/其他失败% 独立列(错误构成一目了然;错误率过高时延迟指标不具横向可比性,分析报告会标注幸存者偏差)
- **连接失败快速止损**:某策略全部请求均为连接类错误(连接被拒/超时)且 0 成功时,立即停止剩余策略并标记任务出错,状态栏给出可操作提示(核对地址端口 / 服务器上 `ss -tlnp` 确认监听 / 先用「自动检测」验证连通性);连接被拒任意策略触发,超时仅首个策略触发(避免把服务过载误判为不可达)
- **指标看板(10+1 卡)**:最近完成策略的关键指标 — 成功率、TTFT P50/P99、总延迟 P50/P99、TPOT(ITL) P50(附 avg/P90/P99)、输入 TPS(prefill)、解码 TPS(decode)、单请求 TPS、聚合吞吐;设置 SLO 后顶部额外插入 **Goodput 卡**(req/s + 达标率 + 阈值条件),「并发扩展性」图表自动叠加 Goodput 曲线
- **Excel 报告**:「汇总」+「分析报告」+ 每策略明细页;文件名带模型名+架构;在线预览、下载、历史报告管理、存储路径可配置;报告头为**环境信息块**(引擎版本/模型路径/部署参数/GPU 硬件/采样参数/Prompt 模式/SLO 阈值);汇总表含 错误分桶%(超时/限流429/5xx/其他)、ITL P50/P90/P99、Goodput(req/s)、SLO达标%、缓存模式、预热丢弃 等列;明细页自动剔除预热请求并在标题标注;分析报告含「错误构成」「Goodput/SLO」结论段
- **AI 压测分析报告**:压测完成后自动把压测数据发给被测模型本身,按固定模板(总体结论/稳定性/延迟/吞吐扩展性/瓶颈定位/优化建议)生成深度分析,写入 Excel Sheet2 并在面板展示
- **模型对话**:独立 Cherry 风格对话框(自带 API/Key/检测/模型选择,可保存多个 API 配置):
  - **文生视频时长可选**:任务类型切到「🎬 文生视频」后出现时长选择器(5/10/15/20/25/30 秒,默认 5s),按所选时长请求服务端
  - **文生图/文生视频产物自动取回**:生成完成后自动把产物文件从服务端取回本机 `generated_media/` 目录,聊天窗口内直接展示/播放(视频支持进度条拖动),并提供下载按钮(位于「多选」右侧)下载原始文件;取回渠道按优先级:b64 直接解码 → 云存储/官方云 http 链接下载 → SGLang `content` 端点(`/v1/images|videos/{任务id}/content`)→ MiniMax 云 `files/retrieve` 换下载链接 → 该主机已配置「SSH 直连监控」时经 SFTP 拉回服务器内部路径文件;全部失败时展示原始输出路径并附失败原因;历史会话中的图片/视频重新打开仍可查看/播放与下载
  - **思维链真实展示**:解析 reasoning_content / thinking / think 标签,可折叠块实时流式展示
  - **联网搜索**:勾选后先快检模型可用性(不可用直接报错不白搜),再由模型语义提炼搜索词,与本地启发式关键词多路搜索(Bing 翻页 / DuckDuckGo 备选)合并去重注入上下文;结果条数可选 5~20(默认 10);消息上方可折叠展示来源列表;对任意 OpenAI/Anthropic/Ollama 接口均生效
  - **消息管理**:每条 AI 回复支持 复制MD / 重新生成 / 删除 / 多选;多选模式可勾选任意多条消息(含用户消息)批量删除
  - **模型选择**:模型输入框带 ▾ 下拉(列出已识别模型),识别出的模型以芯片展示,点击即选
  - **工具栏布局**:任务类型(对话/文生图/文生视频)为分段按钮,位于归档按钮下一行;思维链/联网搜索开关紧贴输入框上方
  - **文件上传**:PDF / Word / TXT / MD / CSV / 代码文件自动提取文本;图片(png/jpg/gif/webp/bmp)以 `image_url`、视频(mp4/mov/webm/mkv/avi/m4v,≤50MB)以 `video_url` 视觉格式发给多模态模型识别(OpenAI 兼容接口)
  - **MD 渲染**:AI 回复按 Markdown 渲染(标题/表格/代码块/加粗)
  - **会话管理**:新建 / 归档到项目名称(按项目分组)/ 取消归档
  - **归档项目管理**:「📦 归档管理」弹窗内可对归档项目重命名、删除项目(连同会话),以及对单个归档会话恢复、改名、删除
  - 发送中变「停止」可中断;宽度拖拽可调(顶部菜单自动避让,最大 80% 屏宽);输入框可扩大
- **ModelUse 模型工作台**(`/modeluse`,主页对话窗「⛶ 弹出全屏」进入,URL 可直接复制打开;千问风格居中对话列 + 欢迎态建议卡片):
  - **六种任务**:💬 文本(可附文档)、👁 看图(图生文)、🎨 文生图、🎬 文生视频(可附参考图/视频/音频 → r2va 参考生视频)、🖼 图生视频(1-2 张首帧/尾帧)、🎤 语音(TTS 合成 / STT 识别);「⚙ 参数」可设系统提示词与采样参数(temperature/max_tokens/top_p/top_k,随会话保存)
  - **MiniMax v2 视频协议全套**:时长(4-15s)/ 分辨率(480P/768P/2K)/ 比例(自适应、21:9~9:16)/ 接口风格(自动/v1/v2)全参数;接口风格自动探测(SGLang `/v1/videos` / MiniMax v1 云 / MiniMax v2),v2 支持 role 标注的 first_frame/last_frame/reference_image/reference_video/reference_audio/base_video 多模态输入;生成完成的视频消息带「🔄 再生成 2K」(v2 regeneration,source_task_id 或 base_video 续作);**所选分辨率/比例/时长真实传递到上游**(v2 按协议原样发送,SGLang 按短边 480P→480 / 768P→768 / 2K→1152 映射);生成的视频/图片消息一行展示「已取回到本地 大小 | 耗时 · 路径 · 尺寸」(尺寸由播放器真实读取,如 1344×768)
  - **技能与提示词工程**:输入 `@` 引用技能(内置翻译/代码审查等,可自定义),输入 `/` 插入提示词模板(支持 `{{变量}}` 填充);「📚 提示词」「🛠 技能」页可管理库
  - **附件即时预览**:📎 上传后图片/视频在输入框内直接显示大缩略图(带文件大小,✕ 移除;视频带 ▶ 标记),文档/音频显示芯片;发送后缩略图保留在消息气泡内
  - **会话管理**:新建 / 重命名 / 合并多会话(按时间顺序拼接)/ 归档(与压测主页项目互通)/ 清空本会话 / 清空全部(双重确认);消息支持 复制 / 重新生成 / ✂ 截断 / 删除 / 多选批量删除;**↩ 撤回为真撤回**(该提问连同回复从数据库删除,原位留一行撤回提示,不再计入对话上下文);用户消息的 撤回/删除/多选 按钮位于气泡底部;从其他页面(网关/工坊等)点击会话历史自动返回对话视图
  - **API 网关**:聚合上游 API 统一对外暴露
    - **渠道**:添加上游(名称/Base URL/Key/优先级),填地址自动检测协议(OpenAI/Anthropic/Ollama)、拉取模型列表、自动勾选能力类型(对话/文生图/视频/语音/嵌入);支持手动「测试」探测(响应时间持久化显示);按优先级故障转移;**表格内开关直接启停渠道**(停用即不参与转发),支持名称/地址/模型搜索,模型列表点击展开
    - **密钥**:生成对外密钥(配额/有效期/启停);**创建与编辑时可勾选绑定渠道、并从所选渠道的模型并集中勾选允许模型**(真实绑定,网关全端点强制过滤,未选渠道请求 403);**表格内开关直接启停密钥**(禁用后调用立即 403),用量进度条与剩余有效天数展示,密钥页统计卡
    - **对外协议**:`/v1/chat/completions`(OpenAI)、`/v1/messages`(Anthropic,请求/响应/流式事件双向转换)、Ollama 上游自动适配;`/v1/embeddings`、`/v1/images/generations`、`/v1/audio/speech|transcriptions|translations`;视频 v2 全套(`/v2/video_generation`、`/v2/query/video_generation[/{task_id}]`、`/v2/video_regeneration`、`/v2/h3_context_ir` 提示词增强、`DELETE /v2/video_generation/{id}` 取消/删除)与 `/v1/videos`(SGLang)透传
    - **日志与统计**:每密钥调用量/渠道分布/成功率,调用日志可清空
  - **动漫工坊**(导航入口仅在任务类型为 🎬 文生视频 / 🖼 图生视频 时显示,普通对话不出现;专业动漫片段生成与剪辑):
    - **片段生成**:描述 + 🎨 动漫风格预设(日系赛璐璐 / 吉卜力水彩 / 国漫水墨 / 赛博动漫 / 漫画分镜 / 像素 / Q版萌系,可多选)+ 🎥 镜头语言预设(远景建立 / 中景跟随 / 面部特写 / 快速运动 / 环绕 / 俯拍升降,可多选)自动组合专业提示词;时长(4~15s)/ 分辨率(480P/768P/2K)/ 比例 / 接口风格全参数,复用对话页选中的数据源与模型,生成完成自动加入时间线
    - **片段库**(右侧):自动列出全部已生成视频(含对话页生成的),缩略图预览、大小与时间,一键加入时间线,可刷新;每项带「🗑 删除」(二次确认后服务端文件一并删除,若在时间线中则同步移除)
    - **剪辑台**(专业时间轴编辑):
      - **预览监视器**:点击任意片段胶片时间轴上的时间节点,监视器即刻从该点播放小段预览动画(播至出点自动停);带「⏮ 入点 / ▶ 播放 / ⏭ 出点 / ⏹ 停止」与时间码,切换页面自动停止播放
      - **胶片时间轴**:每段以 8 帧缩略图胶片条呈现,拖动两端手柄实时裁剪入点/出点(舍弃部分压暗标记,拖动中显示秒数气泡),点选时间点后可用「⏮ 入点」「⏭ 出点」按钮按播放头精确打点;起止秒数字输入仍可直改
      - **序列时间轴标尺**:整线按各段裁剪时长比例排布为彩色片段块,带秒刻度;点击色块定位聚焦对应片段;「▶ 整线预览」按时间线顺序连播全部片段(序列时间码 + 标尺播放头实时跟随),先预览再合成
      - **等距操作排**:每段 6 键等宽一排(⏮ 入点 / ⏭ 出点 / ▶ 片段 / ⬆ 上移 / ⬇ 下移 / ✕ 移除),侧栏「整线预览 / 停止 / 清空」三键等距
    - **时间线合成**:合成时逐段裁剪转码(统一分辨率/帧率/音轨,无音轨自动补静音)后拼接
    - **合成成片**:导出分辨率可选(跟随第一段 / 720P / 1080P / 480P,异形分辨率自动加黑边不变形),可选首尾淡入淡出;成片在页内播放、一键下载,并自动进入片段库(依赖 ffmpeg:PATH 优先,或随 requirements 自动安装的 imageio-ffmpeg)
  - **接入示例**(「📘 接入」页):OpenAI / Anthropic / Ollama 三协议 × Python / JavaScript / Go / Java / curl / C# / PHP 七语言即拷即用代码,Base URL 一键复制
  - **数据存储**:页面全部数据(会话与消息/网关渠道、密钥与调用日志/技能与提示词库)存于项目目录 `modeluse.db`(SQLite,WAL 模式);首次启动自动从旧版 JSON 文件迁移(原文件改名 `.bak` 保留),消息的附件/媒体/指标等扩展字段无损往返;技能/提示词种子数据内置,空库自动初始化
- **模型部署站**(`/modelstart`,SSH 直连 GPU 服务器做容器化部署与运维):
  - **主机管理**:SSH 主机池(密码 / 私钥登录),支持分组(如「GPU 节点」),连通性探测;主机配置存 config.json
  - **容器总览**:自动检测 Docker 容器与 K8s Pod(名字/镜像/状态/端口/创建时间),点击容器查看详情;容器操作 启动 / 停止 / 重启(K8s Pod 由控制器管理,不直接启停);容器日志查看(docker logs / kubectl logs,可设尾行数);容器内**文件浏览**(目录列表/进入/上级)与**命令执行**(stdout/stderr/退出码分离返回);底部 **WebSocket 终端**直连主机或容器内 shell
  - **预设命令**:常用命令/脚本沉淀为预设,带标签/语言(shell·python·node·perl·ruby),四种形式 — ⌨ 单条命令 / 📜 脚本正文 / 📄 上传脚本文件(执行时自动 SFTP 到目标主机 /tmp)/ **☸ K8s YAML(apply)**;预设可**试运行**(选主机立即执行一次看输出);支持按名称/内容/标签搜索
  - **☸ K8s YAML 预设**:粘贴或多文档(`---`)K8s 清单,或上传 `.yaml` 文件(优先于粘贴内容),执行时在目标主机上 `kubectl apply -f`(粘贴正文走 stdin 不落盘;上传文件先 SFTP 到 /tmp);试运行与编排步骤均可填命名空间(传 `-n`,清单内已声明 `metadata.namespace` 的资源以清单为准)
  - **任务编排**:多步骤方案顺序执行;每步可选**单主机**或**分组**(组内多主机线程池并行,可关);每步选预设或直接填命令,可设「失败继续」;运行中实时日志(逐行流式上屏)、**暂停/继续/撤回**;步骤失败自动给出错误码原因提示;全部运行记录持久化到**执行历史**(含每次运行的步骤状态与完整日志,服务重启后仍可回看)
  - 高风险命令(kill/停止/删除/重启)运行前弹确认;预设/方案/执行历史按账号隔离
- **GPU 监控**:顶部「🖥 GPU监控」支持两种方式:
  - **SSH 直连(推荐,免部署)**:填 GPU 服务器的 IP / SSH 端口 / 用户名 / 密码,本服务直连主机执行 `mthreads-gmi`,实时查看每卡利用率/显存/温度/功耗/频率/PCIe/ECC、GPU 进程表、**驱动与内核模块状态**、拓扑矩阵(1 小时历史曲线);密码明文存于本机 config.json
  - **URL 型**:在服务器部署 gpu-monitor 服务后按地址内嵌看板(见 `gpu-monitor/` 目录)
  支持多主机 tab 切换、连通性探测
- **账户体系**:首次启动自动创建总管理员(默认 `admin / admin123`,登录后请立即改密);管理员可创建/停用/删除账号并授予角色 — 总管理员 / 管理员 / 子账号(按模块授权:压测工作台、模型工作台、模型部署站、SSH 主机、网关)/ 预览用户(全站只读 + 可用对话);子账号数据(API 配置/预设/方案/执行历史/报告目录)按账号隔离;自助注册需管理员审批
- **个性化**:右上角深/浅双主题切换(localStorage 记忆,品牌色 Indigo)+ 字号调节(小~特大);「📖 帮助」查看本说明;「🐍 小游戏」压测等待时玩;旧版原生页面保留于 `/legacy-index` 与 `/legacy-modeluse`(一个版本后移除)
- **压测深空补给动画**:压测任务运行时,全屏背景上演星际补给场景 — 歼星舰(舰桥双护盾球 + 三引擎蓝焰)从星域外进场,悬停于轨道燃料站旁,输能光束持续注入,HUD 与全屏能量条实时显示压测真实进度(已完成请求/总请求);任务结束光束断开,引擎喷焰加速、速度线掠过,飞船跃迁离场(所有皮肤通用,刷新页面后仍按任务状态恢复)

## 快速开始

### Windows

双击 `start.bat`(需要 Python 3.9+,未安装会提示)。

### macOS / Linux

```bash
chmod +x start.sh
./start.sh
```

### 手机 / 平板访问

启动后命令行打印局域网地址(如 `http://192.168.x.x:8765`)并显示二维码,同一 Wi-Fi 下扫码即可访问。

## 使用流程

1. 左侧填 API 地址 + Key → 点「自动检测」(或从「已保存 API」下拉选择)
2. 确认模型;点「模型详情」看服务元数据;选任务类型(对话/文生图/文生视频)
3. 编辑测试策略(或点「智能添加策略」按上限自动调整)
4. 点「创建压测任务」→ 右侧任务列表出现「待启动」任务
5. 点任务卡片 → 点「▶ 启动压测」开始
6. 运行中可「暂停」改剩余策略再「继续」,或随时「停止」
7. 完成后看规则分析 + AI 深度分析报告面板,点「预览报告」在线看,或「下载 Excel 报告」

## 受控对比测试指引(引擎/模型 A vs B)

横向对比的数字只有在**变量受控**时才有意义。按以下顺序配置,两轮测试除被测对象外其余全部一致:

1. **固定环境**:点「自动检测」记录引擎版本与部署参数;在「GPU/硬件环境」填写硬件说明 —— 两者都会写入报告头,事后可溯源。
2. **固定采样**:设置 temperature(对比建议 0)与 seed,固定解码路径;留空则两轮的随机性不可比。
3. **固定负载**:两个任务用完全相同的 ISL(输入tok)/OSL(输出tok)/语言/并发/请求数。TTFT 强依赖输入长度、TPOT 强依赖输出长度,长度不同则延迟数字没有可比性。
4. **控制缓存**:引擎对比用「冷」缓存模式(每请求唯一前缀,前缀缓存必未命中,测的是纯引擎 prefill/decode 能力);评估缓存收益时冷/热各跑一轮对比差值。随机池模式只是"大概率不命中",报告对比请显式用冷模式。
5. **设 SLO 得 Goodput**:设置 TTFT≤X 与 TPOT≤Y 阈值后,汇总表/分析报告以 Goodput(满足 SLO 的 req/s)与达标率为最终结论 —— 单纯吞吐可以用大并发"刷"出来,但延迟全超标就没有意义。
6. **看曲线不看单点**:用「📈 并发扫描」生成 1~64 多档并发,看「并发扩展性」图(聚合 TPS / TTFT P50 / Goodput 随并发变化),拐点即最优并发;单点对比容易误判。
7. **预热**:每组设 2~5 个预热请求(仅执行不计入统计),剔除建连与冷启动抖动;P99 要有统计意义,每组请求数建议 ≥200。

**结果解读注意**:某策略错误率 ≥5% 时其延迟指标不具横向可比性(幸存者偏差,分析报告会标注);错误构成(超时/限流429/5xx)直接指向瓶颈类型 —— 限流为主说明继续加并发无意义,超时为主说明已达承载上限。

## 指标说明

| 指标 | 含义 |
|------|------|
| TTFT | 首 Token 延迟(avg/P25/P50/P75/P90/P95/P99);强依赖输入长度(ISL),对比时必须同 ISL |
| TPOT(ITL) | 流式输出逐 token 间隔(avg/P50/P90/P99);强依赖输出长度(OSL) |
| E2E 总延迟 | 单请求端到端时长 avg/P50/P90/P99(非流式场景的用户感知口径) |
| 输入 TPS | Prefill 速度 = 输入 tokens / TTFT |
| 解码 TPS | Decode 速度 = 输出 tokens / (总延迟 - TTFT) |
| 单请求 TPS | 单请求输出 tokens/秒(用户体验口径) |
| 聚合 TPS | 该策略总输出 tokens / 真实墙钟(整轮首请求发出 → 最后一个请求结束;分批执行时显著小于单请求 TPS × 并发) |
| RPS | 每秒完成请求数(真实墙钟口径) |
| Goodput | 每秒满足 SLO(TTFT≤X 且 TPOT≤Y)的有效请求数;失败请求计入分母 — 横向对比的最终口径 |
| SLO 达标率 | 满足 SLO 的请求占比 |
| 错误分桶 | 超时 / 限流429 / 服务端5xx / 客户端4xx / 连接异常 / 空响应 / 其他 |
| 预热丢弃 | 前 N 个请求仅执行不计入任何统计(剔除建连/冷启动抖动) |
| 缓存模式 | 默认(随机池/固定跟随全局)、冷(唯一前缀,强制未命中)、热(固定同文,全命中) |

---

## 支持与反馈

使用中遇到问题或有功能建议,联系:**marxyong@126.com**

---

<details>
<summary><strong>🔧 部署手册(点击展开:从传包到开机自启全流程)</strong></summary>

## 部署手册

### 一、获取安装包

在已装好环境的开发机上执行:
```bash
python package.py
```
生成 `llm-benchmark-tool.zip`(约 3MB,含自托管前端框架)。把 zip 拷到目标主机(U 盘 / 网盘 / scp 均可):
```bash
scp llm-benchmark-tool.zip user@目标机:/opt/
```

### 二、解压安装

**Windows:**
```powershell
# 右键 zip → 全部解压;或命令行:
Expand-Archive llm-benchmark-tool.zip -DestinationPath D:\llm-bench
cd D:\llm-bench
```

**Linux / macOS:**
```bash
unzip llm-benchmark-tool.zip -d /opt/llm-bench
cd /opt/llm-bench
chmod +x start.sh
```

### 三、安装依赖(首次运行自动完成)

启动脚本会自动创建 venv 并安装依赖,无需手动执行。若想手动装:
```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

依赖清单:fastapi、uvicorn、aiohttp、openpyxl、pydantic、qrcode、pypdf、python-docx、python-multipart、paramiko(SSH 直连 GPU 监控)。

### 四、启动

**Windows:** 双击 `start.bat`

**Linux / macOS:** `./start.sh`

**手动启动(任意平台):**
```bash
venv/bin/python server.py        # Windows: venv\Scripts\python server.py
```

启动后:
- 本机自动打开浏览器 `http://127.0.0.1:8765`
- 命令行打印局域网地址 + 二维码(手机扫码访问)
- 默认端口 8765,被占用自动换端口

**服务器无浏览器环境**(设置 `BENCH_NO_BROWSER=1` 跳过自动开浏览器):
```bash
BENCH_NO_BROWSER=1 venv/bin/python server.py
```

### 五、开机自启

#### Windows(任务计划程序)

```powershell
# 以管理员身份运行 PowerShell:
schtasks /Create /TN "LLMBenchmark" /SC ONLOGON /TR "D:\llm-bench\venv\Scripts\pythonw.exe D:\llm-bench\server.py" /RL HIGHEST /F
```
或图形界面:任务计划程序 → 创建任务 → 触发器「登录时」→ 操作「启动程序」选 `venv\Scripts\pythonw.exe`,参数 `server.py`,起始于 `D:\llm-bench`。

#### Linux(systemd)

```bash
sudo tee /etc/systemd/system/llm-bench.service <<'EOF'
[Unit]
Description=LLM API Benchmark Tool
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llm-bench
ExecStart=/opt/llm-bench/venv/bin/python server.py
Restart=on-failure
RestartSec=5
Environment=BENCH_NO_BROWSER=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now llm-bench   # 开机自启 + 立即启动
sudo systemctl status llm-bench         # 查看状态
journalctl -u llm-bench -f              # 看日志
```

#### macOS(launchd)

```bash
cat > ~/Library/LaunchAgents/com.llm-bench.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.llm-bench</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/llm-bench/venv/bin/python</string>
        <string>server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/opt/llm-bench</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BENCH_NO_BROWSER</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.llm-bench.plist   # 加载并启动
launchctl list | grep llm-bench                             # 查看状态
launchctl unload ~/Library/LaunchAgents/com.llm-bench.plist # 停止
```

### 六、Docker 部署(服务器推荐)

```bash
cd /opt/llm-bench
docker build -t llm-benchmark .
docker run -d --name llm-bench --restart always \
  -p 8765:8765 \
  -v /opt/llm-bench/reports:/app/reports \
  llm-benchmark
```
`--restart always` 即容器随 Docker 自启(需 `systemctl enable docker`)。访问 `http://<服务器IP>:8765`。

### 七、防火墙

- Windows 首次启动弹「允许应用通过防火墙」请勾选,否则局域网无法访问
- Linux: `firewall-cmd --add-port=8765/tcp --permanent && firewall-cmd --reload` 或 `ufw allow 8765`
- 云服务器:安全组放行 8765 端口(注意公网暴露风险,建议仅内网使用)

### 八、常见问题

| 问题 | 处理 |
|------|------|
| 双击 start.bat 闪退 | 用 cmd 打开看报错;确认已装 Python 3.9+ 并勾选 Add to PATH |
| venv 从别的电脑拷来失效 | 启动脚本会自动检测并重建 |
| 手机无法访问 | 同一 Wi-Fi;防火墙放行;用命令行打印的局域网地址 |
| 端口被占 | 自动换端口,看启动日志打印的实际地址 |
| 报告想存别处 | 主页面左下角「报告存储路径」改为主机任意目录,配置存 config.json |
| 其他问题 | 联系 **marxyong@126.com** |

### 九、文件说明

| 文件 | 说明 |
|------|------|
| `server.py` | 后端(协议识别/探测/多任务压测/AI 分析/报告/对话代理/ModelUse 网关与 v2 视频接口/模型部署站 SSH 容器运维·预设·编排/账户体系/内嵌游戏) |
| `static/index.html` | 主压测页(Vue 3 应用入口,import map 引用 vendor 框架) |
| `static/modeluse.html` | ModelUse 模型工作台独立页(Vue 3 应用,千问风格对话 + 网关管理 + 接入示例) |
| `static/modelstart.html` | 模型部署站独立页(Vue 3 应用,主机/容器总览 + 预设命令 + 任务编排 + 执行历史) |
| `static/app/` | 前端 ES 模块:主压测页(`ix-*.js`:API 配置/策略/KV 测试/任务面板/对话抽屉/GPU/壳)、工作台(`mu-*.js`)、部署站(`ms-*.js`),共享 `api.js`/`fmt.js`/`markdown.js`/`theme.js` |
| `static/vendor/` | 自托管前端框架发行文件(固定版本:Vue 3.5 / Element Plus 2.9 / ECharts 5.6,零构建离线可用) |
| `static/legacy-index.html`、`static/legacy-modeluse.html` | 旧版原生 JS 页面(回滚与对照用,路由 `/legacy-index`、`/legacy-modeluse`,一个版本后移除) |
| `requirements.txt` | Python 依赖 |
| `start.bat` / `start.sh` | 启动脚本(自动建 venv 装依赖) |
| `package.py` | 打包为分发 zip(只含部署必需文件,排除运行数据) |
| `Dockerfile` | 容器部署 |
| `tests/` | 冒烟 / 接口 / 权限 / detect 解析等自测脚本(先启动服务再跑,`python tests/smoke_test.py` 等) |
| `tools/mock_server.py` | Mock LLM 上游(OpenAI / Anthropic / DeepSeek / 慢速四种模式,联调与冒烟测试用) |
| `config.json` | 界面配置,自动生成;可选 `web_search_engine`(auto/bing/duckduckgo/searxng)、`searxng_url`(自建搜索实例)、`web_search_count`(1-10 条)、`gpu_monitors`(URL 型 GPU 监控地址列表)、`gpu_ssh_monitors`(SSH 直连 GPU 监控主机)、`modelstart_hosts`(部署站 SSH 主机池,**含密码,请勿外传**) |
| `users.json` | 账户体系数据(自动创建;存盐值+密码哈希,请勿外传) |
| `userdata/` | 按账号隔离的运行数据(报告目录等,自动创建) |
| `gpu-monitor/` | MTT 显卡监控服务部署包(`server.py` + `install.sh`),部署到显卡服务器后由本工具页面内嵌;含 ncuprof CUDA 剖析器源码 |
| `modeluse.db` | SQLite 数据库,存主页与 ModelUse 全部数据(会话与消息/网关渠道、密钥与调用日志/技能与提示词库),自动创建;旧版 `chat_sessions.json` / `gateway_config.json` / `modeluse_library.json` 首次启动自动迁移后改名 `.bak` 保留 |
| `generated_media/` | 对话面板文生图/文生视频产物本地存储(自动生成;删除后历史消息将无法回放/下载) |
| `reports/` | 报告默认目录(可在界面改) |

</details>
