# Sidelit 原生点评适配器

入口 `myeval/adapters/sidelit.py` 调用 Sidelit `POC/NativeCritique/Evaluation/main.swift`，共用 App 的 `CritiqueRuntime.swift`、资源租约和 `SCritique.cpp`。MyEval 只传图片、profile ID 和意图，不复制提示词、不替换指定 Photography Critique 模型。先由 Sidelit `POC/NativeCritique/Evaluation/build.sh` 构建；默认产物位于 `.acceptance/myeval-v2-20261118/build/sidelit-critique`。

`taskpacks/sidelit-v2-development.json` 包含 coffee、chelsea、astronaut 三张已见公开照片及三个配置。作者基线保持原提示；A 增加证据约束；B 在 A 上应用显式用户意图。三配置保持同模型、448 长边、seed 42、temperature 0.1、512 output tokens。baseline/A 不接受意图，不能把其意图维度结果解释为接收到意图后不遵从。每张照片在三个配置间使用相同意图，不同意图或变式仍按原图家族统计。

## 输入与来源

案例必须声明 `source.type=licensed_public`、`synthetic=false`、来源 URL、逐图许可、署名和 `media_sha256`，并在 input 中声明 image_path/image_sha256/intent。默认只读取 Sidelit `POC/Resources/Samples` 下文件；额外公开材料根目录由 `MYEVAL_SIDELIT_PUBLIC_MEDIA_ROOTS`（冒号分隔）显式配置。解析符号链接后校验目录，图片不得超过 30 MiB，输入哈希必须匹配来源哈希和实际文件。意图最多 800 UTF-8 字节，不得包含模型控制标记或 U+0000。不会下载媒体或读取私人相册。

启动前将已验证原始图片逐字节快照至 attempt 的 `media/<sha>.<ext>`，native 读取快照；结果及 review_output 的 media 数组提供相对路径、哈希、许可和署名，供原图附件、导出及视觉评分使用。原外部路径只保留在来源清单。

`MYEVAL_SIDELIT_ROOT` 可指定项目位置；`SIDELIT_CRITIQUE_MODEL_DIR` 可指定两份锁定 GGUF 的目录。模型 expected SHA 来自真实 Swift 源码。describe 只记录文件大小、mtime、expected SHA；native 每次加载校验完整权重。源码、二进制、构建 manifest、native dylib 和适配器均哈希冻结。构建 manifest 的六个源文件、二进制、桥库和实际运行时 dylib 均逐一匹配，陈旧构建不可运行。没有 Git 的项目 git_head 为 null。worker 及取得资源锁后的二次 describe 均阻止源漂移；漂移时须新建实验，不能覆盖旧 attempt。

## 进程和证据

一模型文件锁跨 MyEval worker 串行。排队计入 trial 超时；native 继承锁 fd，异常 worker 退出后仍不会释放正在执行的模型锁。每次独立 native 进程，不能把它描述为跨 trial 暖模型；权重校验会预读文件，不是磁盘冷启动。进程在拒绝网络的 macOS sandbox 下运行。

协议版本 `sidelit-critique-eval-v1`，request 包括 image_path/image_sha256/profile_id/intent/request_id/output_dir/cancel_path/timeout_seconds/parent_pid。返回 native_run、parsed_report、status、error、profile、limitations。parent_pid 供 native 检测父进程丢失。取消先创建 native 标记，3 秒后 TERM，5 秒后 KILL；原生取消确认来自 CLI 的显式 cancellation.cooperative_acknowledged，与 terminal status 分开；合作超时也可确认，强制进程退出本身不能证明原生响应。保留 requested_reason、native_status、native_call_returned 及本地信号记录。强制退出与合作取消分开。所有 native 输出、请求、媒体清单、stdout/stderr 和事件保存在 `ctx.root` 下的忽略数据目录，原文保留、不记录隐藏思维链。不要把实际运行输出提交 Git。

## 评分与限制

独立量表 `rubrics/sidelit-v2.json` 涵盖可见事实、技术依据、意图适切、可操作性、审美意见边界与非空泛性。评阅者必须实际打开哈希对应的原图；仅有路径和文字的 AI 评分不算视觉质量验收。应提供原图附件与匿名输出并排查看，并保留评阅引用、图像区域及分歧。模型的 overall_score 和维度 score 是被评内容，不是裁判。

自动规则只判断 native 终态与产品解析，不评价视觉事实。token limit、schema failure、取消、超时、加载失败必须留在运行分母。3 个已见来源只能做开发筛选；留出由独立材料方维护，候选作者不能读取。真实 iPhone、相机、快门、持续温升和能耗均 unsupported，Mac 本地推理不代表这些能力；本地无远程 token 账单也不等于完整成本为零。

当前接入代码与模型无关描述检查不替代真实 native canary、连续浏览器录屏或质量验收；相关运行证据由主脑单独记录。

## 开发轮次

第一轮 9 次开发运行已结束，包含失败，原始记录保留在 round1，不被后续结果覆盖。已进行本轮唯一一次最终开发提示修订：作者基线版本 1.0.0，两项候选版本 1.1.0，profile ID 保持不变，任务包版本 1.1.0。最终源码冻结和修订后验收仍待完成；不能把首轮失败或修订声明当作改进证明。
