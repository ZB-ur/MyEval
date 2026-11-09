# 本机观测平台

首版采用 **Langfuse 4.76.0 + MyEval 标准库 OTLP 导出器**。Langfuse 承接通用 trace、观察和实验视图；MyEval 保留原始开放证据、任务执行、重复身份和盲评。没有安装 Opik。此选择已经过本机部署和真实入库验证，仍是单机开发环境，不是生产权限、备份或高可用方案。

## 启动与入口

```sh
brew install colima docker docker-compose # 本机已安装
scripts/platform-start
scripts/platform-status
scripts/platform-stop
```

- UI：<http://127.0.0.1:3300>。初始账号 `local@myeval.invalid`，密码在本机 `.local/platform/secrets.env` 的 `LANGFUSE_USER_PASSWORD`。不在终端、UI 或文档输出密码；可用本机编辑器查看该文件后登录。
- MyEval 项目 ID：`myeval`。首轮验证使用 headless initialization 创建本地组织、项目和用户，不产生邮件或外部账号。
- `platform-stop` 仅停止 MyEval 容器和独立 `myeval` Colima profile，保留数据卷；`platform-start` 可恢复。不要使用 `down -v` 或删除 profile 数据，除非有意销毁本机观测记录。
- 独立配置、随机凭据、容器数据磁盘、日志在 `.local/platform/`；目录权限 700，凭据文件 600。Docker 客户端配置独立，未切换用户默认 Docker context，未修改全局安全设置。
- Colima：4 CPU / 8 GiB RAM / 20 GiB 容器数据磁盘 / 8 GiB 系统磁盘，`vz`，不挂载宿主 home。容器日志按 10 MiB × 3 轮转。启动不注册开机服务。
- 只映射 `127.0.0.1:3300`（Web）和 `127.0.0.1:3390`（本机对象存储）。Postgres、ClickHouse、Redis、worker 不暴露宿主端口。关闭 Langfuse 遥测、应用内 Agent 和 AI 网关。
- 本次安装：Colima 0.10.3、Lima 2.3.0、Docker CLI 29.9.3、Compose 5.7.0；macOS / arm64。实际镜像 digest 固定在 `infra/compose.yaml`，核对表为 `infra/images.lock.json`。其他宿主平台未验收。

## 公共层接口

```python
from myeval.observability import publish_trial, verify_publication
publication = publish_trial(trial, events, data_dir)
readback = verify_publication(trial["id"], data_dir, attempt=trial["attempt"])
```

默认从 `.local/platform/client.json` 读取专用平台项目凭据；可用 `MYEVAL_LANGFUSE_CONFIG` 指定另一份本机配置。导出只允许 loopback URL，显式绕过系统 HTTP 代理，拒绝重定向。没有业务模型密钥、儿童资料或原产品数据库读取逻辑。

每个已完成 trial attempt 导出一条根观察和逐条可观察事件。trace、span 和回执身份均包含 trial ID 与 attempt；重试保持同一独立样本，并保留前次失败 trace。调用方只传入本 attempt 的事件。事件只有真实观察时间时显示为零时长事件，不虚构完整阶段耗时。配置请求值、观察到的有效值、用量、费用及未知项分别保留在元数据。未知费用不发成 0；未将未知模型填进平台价格表。Langfuse 原生 token/费用汇总不是本首版的成本真相，应看 MyEval 原始字段。

Langfuse 中一个实验对应 MyEval 的 `experiment_id + configuration_id + repeat_index + attempt`，每项是稳定 case ID；原 MyEval experiment ID 同时作为 session。这样同一 case 的不同配置、重复和重试不会被平台合并为一个独立样本。版本、配置、status、episode/turn/stage/attempt 元数据随每条观察传递。MyEval 的规则、模型评价、人工盲评不混成平台总分；当前导出在根观察元数据中保留规则评价，人工评价仍以 MyEval 存储和导出为准。

`published` 只表示 OTLP 接口确认接收；`verify_publication` 另行检查实际读回。回执存于 `data_dir/platform/receipts/`：明确已接收的 payload 不重复发送；再次调用返回 `already_published` 及当前导出 payload 是否相同。网络响应中断后，接收状态可能未知，此时只读回查证，不能盲目重试产生重复观察。确定连接拒绝或明确 4xx 拒绝可重试。所有发布错误独立返回，已保存的业务 trial 不被改写成执行失败。

## 已实测证据

2026-11-09，证据在忽略目录 `.local/platform/acceptance.json`、`canary-trial.json`、`images.json` 及运行日志。没有新增单元测试或断言验收脚本。

- `/api/public/health` 返回 HTTP 200，版本 4.76.0。
- 项目 Basic Auth API 返回 `myeval / MyEval`；通过真实 CSRF + credentials 登录，session 确认为本地用户。密码未写入命令行或输出。
- 合成连通性 trial：`platform-transport-canary-20261109`，明确未调用业务模型，不是 Sijian/pome 质量结果。
- OTLP trace `0f2bf6d9182b80f263cb6f5864e47f14` 返回 200，读回 3/3 观察、0 重复 ID；根观察及两条子事件的 parent、输入输出和实验关联可查。
- 实验 API 返回 `c6c0774592ae51b546f0b1cbb4752ae4`，itemCount = 1；名称 `platform-transport-only-20261109:local-otel-v4:0:1`。
- 同一 fixture 再次发布返回原回执、`already_published = true`，没有再次发送。早于后续导出元数据完善的 canary 保持原样；不能重发旧 span 来覆写它。
- attempt 身份补充：`platform-attempt-identity-20261109` 的 attempt 1 / 2 分别产生 trace `82d08b2330fd932a344a5b7c2aff388e` / `ac84093ba30a7b3996a728aff0cab7fe`，各读回 2/2 观察，重复发布均命中各自回执。两条均为明确标识的传输样本，只验证身份隔离，不充当真实业务失败/重试证据。

已用 `platform-stop` 停止全部六容器和 VM，再用 `platform-start` 恢复；health 200，原 trace 仍读回 3/3 观察、0 重复，根子层级与输入输出保留。上述 canary 仅证明平台链路。

正式业务矩阵的只读入库复核（2026-11-09）：

| MyEval 实验 | 项目 | trial 可见 | 观察读回 / 预期 | 平台实验组 |
| --- | --- | --- | --- | --- |
| `60cb6073-8797-4fd1-85fc-488e0f6694c4` | pome | 8 / 8 | 487 / 487 | 4 |
| `70a79bdd-e4b9-4660-8b26-584b8283da4c` | Sijian | 6 / 6 | 102 / 102 | 2 |

第一轮查询已完整匹配，无需第二轮。14 个 trial 均为真实产品路径执行后的已发布结果；每个 trace 的全部观察 ID 均读回，共 589 条、0 重复 ID。6 个实验组的名称、项目数据集和 itemCount 匹配配置/重复/attempt 身份；14 个实验条目的 trace、case 和项目归属逐项对应，无串组。详细 trial/trace/平台实验 ID、数量和身份复核结果保留在忽略目录 `.local/acceptance/platform-business.json`，未将业务输出或凭据写入文档。材料仍是虚构样本；本项只证明平台证据同步与组织正确，不判断业务内容质量或用户偏好。

## 选择依据与限制

采用官方 Docker Compose 的六个必要服务并固定 release/digest，去掉首版不需要的网关；使用官方 headless 初始化避免手工配置漂移。[官方部署](https://langfuse.com/self-hosting/deployment/docker-compose)、[初始化变量](https://langfuse.com/self-hosting/administration/headless-initialization)。

当前 v4 使用 `/api/public/otel/v1/traces` + `x-langfuse-ingestion-version: 4`，根观察保留输入输出并把实验/项目上下文传播到每条事件；不用已弃用的旧 trace ingestion 路径。标准库适配只承担有限的本地完整 trial 批次，无 SDK 自动插桩、媒体上传或流式更新；将来扩展这些能力应改用官方 SDK。[OTLP 与属性映射](https://langfuse.com/integrations/native/opentelemetry)、[v4 迁移与不可变观察](https://langfuse.com/integrations/native/opentelemetry/migration-to-v4)、[本地实验关联](https://langfuse.com/integrations/native/opentelemetry/experiments)。

目前没有第三方模型评分器、Langfuse 内人工盲评、生产备份/恢复演练、远程访问或用户权限体系。MyEval 的匿名配对页面负责评审偏差控制，普通 Langfuse 管理页会显示配置，不能用于盲评。
