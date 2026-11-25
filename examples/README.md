# 用 CLI 完成一次比较

这组示例从导入材料开始，执行、等待、查看并导出一轮完整实验。所有回复都来自仓库内预编写的虚构演示内容，不调用模型，不需要 API 密钥。

| 文件 | 用途 |
| --- | --- |
| [office-dataset.json](office-dataset.json) | 两个办公案例的数据集，版本 `1.0.0`，均为 `development` 划分。 |
| [office-comparison.json](office-comparison.json) | 固定使用该数据集版本，比较“原文摘要、行动清单、依据优先”三个已注册方案。 |

案例取自 [demo-office-v1 任务包](../taskpacks/demo.json) 的会议安排与上线计划，保留原创虚构材料、预编写回复 `demo_outputs` 和两个独立家族。数据集只引用已注册配置；导入不会修改正式任务包，也不会执行任何运行。

## 1. 启动空工作区

需要 Python 3.11+。在仓库根目录的第一个终端运行：

```sh
python3 -m myeval --port 8335 --data-dir .local/cli-example --concurrency 1
```

使用新的 `.local/cli-example` 目录，可与默认工作区及 `scripts/demo.py` 的演示目录分别保存。服务只监听本机；如果 8335 已被占用，换一个端口即可，CLI 会从同一数据目录读取实际地址。

## 2. 导入、执行、等待与导出

在仓库根目录的第二个终端依次运行。变量只保存实验 ID，凭据由 CLI 从数据目录读取，不需要复制到命令行。

```sh
scripts/eval --data-dir .local/cli-example datasets import examples/office-dataset.json \
  > .local/cli-example/dataset-import.json

scripts/eval --data-dir .local/cli-example start examples/office-comparison.json \
  > .local/cli-example/created.json

MYEVAL_EXPERIMENT_ID=$(python3 -c 'import json; print(json.load(open(".local/cli-example/created.json"))["id"])')

scripts/eval --data-dir .local/cli-example wait "$MYEVAL_EXPERIMENT_ID" --timeout 60 --poll 0.2 \
  > .local/cli-example/completed.json

scripts/eval --data-dir .local/cli-example show "$MYEVAL_EXPERIMENT_ID" \
  > .local/cli-example/detail.json

scripts/eval --data-dir .local/cli-example export "$MYEVAL_EXPERIMENT_ID" \
  --format zip --output ".local/cli-example/exports/$MYEVAL_EXPERIMENT_ID.zip"
```

导入结果在 `dataset-import.json`，首次导入的 `import_status` 为 `created`，同内容再次导入为 `unchanged`。版本被固定保存；同名版本不能覆盖不同内容。

`completed.json` 与 `detail.json` 应显示 `status: "completed"`、`counts: {"succeeded": 6}`、`case_count: 2`、`family_count: 2`。规模是 **2 案例 × 3 方案 × 1 次 = 6 次运行**，实验并发上限为 1，每次执行超时为 30 秒。此次没有选入故障演练案例。

`wait` 返回 0 表示执行完成且没有执行异常，不表示回复质量已通过。等待超时只停止等待，不取消实验。导出会返回文件路径、字节数和 SHA-256；ZIP 保留输入、冻结配置、逐次尝试、事件与源码快照。导出目标已存在时会拒绝覆盖，重复练习可新建实验，得到新的 ID 和导出路径。

需要直接查看界面时运行：

```sh
scripts/open --data-dir .local/cli-example
```

## 3. 停止后继续查看

在第一个终端按 Ctrl+C 停止服务，再用原启动命令启动。实验、数据集版本和尝试记录会保留；服务不会自动新建实验。第二个终端可再次执行上述 `show`，或列出全部演示实验：

```sh
scripts/eval --data-dir .local/cli-example experiments --project demo
scripts/eval --data-dir .local/cli-example datasets show office-cli-example --version 1.0.0
```

修改示例材料时，给数据集新的版本号，并同步 `office-comparison.json` 中的 `dataset_version_id`。不要把已公开的开发材料改称未见过的留出材料。方案参数仍来自已注册任务包；接入自己的 Agent 请看[适配契约](../docs/adapter-contract.md)。

本例只验证平台流程与可观察规则，不提供 AI 评分、真人偏好或模型优化结论。模型用量和费用保留为空。完整命令及退出码见 [HTTP / CLI 参考](../docs/api-v2.md)。

2026-11-25 已在 macOS、Python 3.14.3 的独立源码副本与空数据目录实际执行以上流程：导入 1 个数据集版本，6/6 次运行成功，ZIP 校验通过。重复导入返回 `unchanged`；服务停启后仍为 1 个实验、6 次运行、6 次尝试，数据集版本与内容哈希不变。没有模型调用、AI 评分或真人评审记录。
