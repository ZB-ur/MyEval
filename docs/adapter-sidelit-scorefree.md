# Sidelit 无分数 V2 接入

本适配器新增显式 `parameters.protocol_version=sidelit-critique-eval-v2` 路径。未指定协议继续使用旧 V1。V2 固定调用 Sidelit `.acceptance/myeval-scorefree-20261118/build/sidelit-critique-v2`，桥库为 `libSCritiqueV2.dylib`；没有模型替换、MyEval业务提示词或失败回退。新CLI不等于App已经接入无分数入口。

## 配置与材料

新任务包 `sidelit-scorefree-development-v1`：作者对照 `sidelit-scorefree-author-control-v1`、单次候选 `sidelit-scorefree-single-v1`、两阶段候选 `sidelit-scorefree-evidence-first-v1`。分别对应 author-schema-v1、score-free-observation-v1、score-free-evidence-first-v1，均显式使用V2协议。作者对照仍返回完整旧分数report并忽略意图；A/B共用无分数契约。协议修复的收益与视觉内容改善必须分开，不能将基线没有能力接收的约束解释为相同输入下不遵从。

任务包逐字保留计划中12条新编虚构用户原话，仅配coffee、chelsea、astronaut三张已见公开图。照片不是合成数据，保留原许可、署名、SHA。12意图只算3个图像家族。preserve/avoid只摘取请求明示约束，独立核查重点和expected_behavior不进入请求。四个额外路由检查由主脑另行执行，不计入图像质量样本。

## 请求与版本

顶层保留图片路径/哈希、请求ID、profile、输出目录、取消标记、总期限和父进程ID；新增 `product_request`：purpose、intent（text/preserve/avoid）、response_language、rating_request。适配器要求原始input.intent与product_request.intent.text一致，只转发该显式对象，产品共用层负责严格字段验证和评分/冲突路由。没有从自由文字推断rating_request。任务包response_language固定en、rating_request为null。评分请求及冲突由产品返回unsupported或invalid request，不静默回退作者协议。

V2 manifest严格匹配7项源码：Runtime、Lease、CritiqueContractsV2、Bridge h/cpp、EvaluationV2 main/build；同时匹配二进制、桥库和实际runtime dylib哈希。describe记录适配器hash、模型expected SHA/大小/mtime，完整权重校验仍由native加载负责。旧V1使用独立原6项源码集，避免新共享文件被误加入旧manifest。共享源变动仍会正确使旧构建变陈旧；不改写旧冻结记录。

## 执行与证据

V1/V2共用同一文件锁，避免两个模型进程同时占用资源；图片先复制为attempt内不可变原字节快照。保留网络禁用、父进程监视、取消标记后TERM/KILL、资源释放及显式合作取消确认。总trial期限跨两阶段共用，不因第二次调用重置。

原始封套保留 output_contract、schema_version、schema_status、semantic_disposition、native_runs、stages、取消和资源释放；不将parser有效等同视觉事实正确。native_run_paths逐项声明为attempt内artifact，失败已有证据也保留。中间可核实观察是产品诊断证据，不是隐藏思维链，不进入最终盲评对象。

V2 `review_output`固定只有media、原始intent、完整最终report、status。作者对照同样保留完整旧report，不能删除分数使它看似遵从。失败没有加工/修补后的业务对象，raw仍在output与native artifacts；rendered_feedback是共享产品投影，不作为本轮评分对象。旧V1 review_output语义不变。

usage累加所有native_runs的prompt/completion tokens及prompt/generation/end-to-end时间，记录实际native_calls；load_ms跳过configuration.modelWasLoadedBeforeRequest为true的暖调用，RSS取进程峰值最大值。两阶段不计作独立质量样本，也不隐藏额外成本。阶段本身的原始计时仍可追溯。

## 验收与限制

量表 `rubrics/sidelit-scorefree-v1.json` 分别评价无分数/请求契约、视觉事实、原因与未知边界、用户约束、有效覆盖及条件建议。评阅必须实际打开原图快照，不能仅凭图片路径判视觉质量。合法grammar、有效ID引用和可解析JSON不证明内容真实；文本中偷偷评分仍属语义违约。失败、拒绝、insufficient_evidence、取消、超时及不支持均单列，不能靠弃答抬高质量。

适配器初交付只完成静态检查；随后主脑已真实构建并执行下述native开发及生命周期验收。物理相机、iPhone推理、产品V2 UI旅程、快门、温升和能耗仍未覆盖。旧任务包、量表、文档和历史运行未修改。

## 边界复审补充

旧V1 describe从动态扫描Shared改为固定原六项编译源码，只恢复manifest与真实编译输入的一致性，不改变旧调用语义。当前共享Runtime/Bridge修改后旧构建判stale是预期保护；需要旧版运行时使用原snapshot恢复对应源与构建，本工作不会重建或覆盖旧build。

V2 recorded_native_runs是已有完整记录数，expected_stages是计划上限；scheduled_stages仅接受CLI明确声明，缺失为null。失败/取消时native_calls为null，不能由记录数推断未记下的load尝试不存在。此时tokens/阶段时间只覆盖已有记录，metrics_scope明确不完整，load_ms为null，不把未知成本当零。structured refusal也必须具有parsed报告和全部成功native状态才作为业务交付；insufficient_evidence单列coverage，结构规则pass不表示内容质量通过。

## 2026-11-18 实际开发结果

首版1.0.0的单次canary返回空观察，严格parser判为failed，未修补。1.1.0将既有schema的非空观察要求加入grammar，保留此前记录；另修复提前拒绝重复metrics及强停遗漏阶段artifact。提示词文字未改。本轮不再继续调参。

| 配置 | 完整结构交付 | 独立AI判断有用且有据观察 | 严重事实/因果错误响应 | 实测中位耗时 |
| --- | --- | --- | --- | --- |
| 作者对照1.0.0 | 12/12 | 7/12 | 4/12 | 5.65秒 |
| 单次候选1.1.0 | 12/12 | 3/12 | 4/12 | 7.29秒 |
| 两阶段候选1.1.0 | 12/12 | 9/12 | 3/12 | 10.10秒 |

两候选均未达到预先10/12有效观察且无新增严重幻觉的门槛，晋级集合为空，新的确认集继续封存。二阶段仍把闭嘴猫说成打哈欠、把拿在身前的头盔说成戴在头上。无评分键是契约能力，不能据此宣称摄影建议质量已经合格。12次作者控制的实际prompt/RGB hash及原文与旧版对应三图完全一致。

上述为3个已见图像家族、12条意图的内部AI盲评，保留完整业务schema导致形状仍可能泄露方案线索；不是人类摄影偏好验收或泛化证据。计时来自同一Mac开发运行，不是iPhone性能、能耗或磁盘冷启动指标。response_language请求en时多条实际返回中文，语言偏好遵从未得到保证。

实际生命周期覆盖显式评分unsupported、模式冲突failed、引用/否定8/10仍走观察、timeout、stage2合作取消、stage2强停保留stage1和随后新attempt成功。强停实际用了SIGTERM、退出-15，合作取消确认未知；不能写成原生确认已取消。timeout和强停的缺失用量记unknown。每阶段sidecar先保存，再调度下一阶段。

完整证据在Sidelit `.acceptance/myeval-scorefree-20261118/`，决策在MyEval `.local/coordination/20261118/sidelit-scorefree-promotion-decision.json`。App未接线；共享V2入口由显式`SIDELIT_CRITIQUE_V2`编译flag启用，旧默认保持author。独立V2 CLI和未启用V2的旧CLI均已实际编译通过，原旧build没有覆盖。
