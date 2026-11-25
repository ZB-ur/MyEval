# Sidelit 当前构建兼容入口

2026-11-18 的共享 Runtime / C++ 更新使原 V1 构建与当前源码不再一致。保留旧任务包、默认构建路径和历史快照，新增显式兼容任务包 `sidelit-v2-development-compat-20261118`，版本 `1.0.0`。它调用已经实建、与当前源码一致的 V1 兼容二进制。没有修改业务提示、模型、默认 profile 或 App UI。

## 构建选择

`describe` 和 `run` 共用 `_select_build`。配置参数只允许 `profile_id`、`protocol_version`、`build_id`；不接受任意构建路径、二进制路径或命令。

| build_id | 协议 | Sidelit 项目内目录 | 当前状态 |
| --- | --- | --- | --- |
| `legacy-frozen-20261118` | V1，原默认 | `.acceptance/myeval-v2-20261118/build` | 与当前共享源码不一致，拒绝执行 |
| `legacy-compat-20261118` | V1，显式选择 | `.acceptance/myeval-scorefree-20261118/legacy-compat-build` | 预检可用 |
| `scorefree-20261118` | V2，原默认 | `.acceptance/myeval-scorefree-20261118/build` | 预检可用 |

V1 / V2 的完整协议分别为 `sidelit-critique-eval-v1` / `sidelit-critique-eval-v2`。省略 `build_id` 仍使用对应协议的原默认值，不会自动绕过旧构建的漂移检查。模型权重仍在 native 加载时完整验哈希；预检只检查权重元数据，不加载模型。

来源冻结包含适配器 SHA、协议、build ID、所选构建目录、精确编译源码、manifest、二进制、桥库和运行时 dylib。manifest 的协议、源码哈希和产物哈希必须匹配。worker 在执行前校验冻结来源；资源锁取得后适配器再校验一次。

## 可用菜单配置

新任务包的三个配置版本均为 `1.0.0`，表示新增的构建绑定入口；`native_profile_version` 单独记录业务版本。原任务包和 scorefree 任务包内容未修改。

| 新配置 ID | 业务 profile | 业务版本 |
| --- | --- | --- |
| `sidelit-author-schema-v1-compat-20261118` | `author-schema-v1` | 1.0.0 |
| `sidelit-evidence-grounded-v1-compat-20261118` | `evidence-grounded-v1` | 1.1.0 |
| `sidelit-intent-conditioned-v1-compat-20261118` | `intent-conditioned-v1` | 1.1.0 |

任务包 `sidelit-scorefree-development-v1` 的基线 `sidelit-scorefree-author-control-v1` 与两个候选 `sidelit-scorefree-single-v1`、`sidelit-scorefree-evidence-first-v1` 也通过当前来源预检。可运行不代表质量合格：两个候选仍未通过既定开发晋级门槛，确认集继续封存。

## 本次验收和接入

- 六个配置均实际执行 `catalog.freeze` 与相应二进制 `--describe`，CLI 退出码为 0；协议、profile 版本和构建来源相符。
- 九个隔离 worker 请求保存了 request / events / result：七种非法构建或路径选择返回 `unsupported`；旧默认构建返回 `native_build_stale_or_missing`；历史 scorefree 快照返回 `source_changed_since_freeze`。全部未产生 `native.started` 或 native request。
- 本次没有模型推理、产品 UI 验收、主服务重启或确认集读取。详细证据位于 `.local/coordination/20261118/sidelit-compat-preflight/`，交接为同目录上级的 `sidelit-compat-ready.json`。

主会话已重启 MyEval 主服务，并在独立恢复副本通过真实UI建立1案例×3配置的新兼容实验，保持暂停、0模型调用，完成菜单与来源冻结验收。主工作区未增加验收项。适配器 SHA 已改变，旧冻结实验保留原来源，不能覆盖快照后继续执行；历史结果继续按只读证据查看或导入。首次新推理仍应单独记录，不能将这次无推理预检当作模型质量或 iPhone 验收。

恢复副本的来源快照为 partial：已归档18份配置对应源码，build.sh 只保存声明哈希，未进入源码字节白名单；依赖与模型权重也不在该快照内。不要将这一来源证明称为可在空机直接重建的完整包。主脑收据为 `.local/coordination/20261118/root-sidelit-compat-ui-acceptance.json`。
