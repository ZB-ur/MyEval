# MyEval 首版本机验收

日期：2026-11-09（Asia/Shanghai）。范围为两个项目的虚构文字材料、真实业务 HTTP 路径、MyEval 中文 UI 和本机 Langfuse。验收使用真实服务、浏览器、进程信号、文件和数据库复核；没有新增单元测试、测试目标或断言脚本。

结论：上述首版范围达到交付门槛；模型内容质量与真实用户偏好仍待独立评价。工作区最终截图为 `.local/acceptance/screenshots/14-overview.png`，母会话的独立结论见 [复核记录](parent-review.md)。

## 入口与运行条件

```sh
cd /Users/lddmay/MyEval
scripts/service start
scripts/open
# 可选观测平台
scripts/platform-start
```

- MyEval：<http://127.0.0.1:8310>；Langfuse：<http://127.0.0.1:3300>。仅绑定本机。停止命令保留数据，重启不自动重发模型请求。
- 本机 Python 3.14.3、macOS arm64；公共服务和 UI 无额外 Python/npm 依赖。产品源码及最小共享接口改动须同时存在，见 [集成补丁](../../integrations/README.md)。产品改动保持未提交，交给各项目整合。
- 主脑恢复后再次从本会话运行记录核对 `gpt-6-astra / max`、`danger-full-access`、`approval_policy=never`；证据 `.local/coordination/resumed-runtime.json`。未修改全局权限配置。
- 原始模型输出、数据库、截图、凭据、浏览器下载均留在忽略的 `.local/`，不随源码提交。本文只记录身份、数量和结论。

## 真实执行与分母

| 实验 | 冻结范围 | 实际结果 |
| --- | --- | --- |
| `60cb6073-8797-4fd1-85fc-488e0f6694c4` | pome 2 案例 × Flash low/high × 2 次重复 | 8/8 业务执行成功；候选、审校、实际 final、下一轮上下文、整理及状态 API 落盘复读均有证据 |
| `70a79bdd-e4b9-4660-8b26-584b8283da4c` | Sijian 发现/有据复盘/材料缺项 3 案例 × Sol low/medium × 1 次 | 6/6 业务执行成功；真实 `/discover`、`/review`、产品校验与原生轨迹保留 |
| `a84774a9-935f-4b14-81ed-83f226fad381` | pome 停止、真实失败与显式重试 | 4 trial、6 attempts；最终 2 成功、2 调度前停止；历史 1 次在途取消和 1 次 HTTP 502 均保留 |
| `a81997cc-faed-4e12-8931-66a051fafef5` | pome 单案例、两配置、5 秒上限，平台实际停机 | 2/2 超时；本地证据与导出可用；平台恢复后仅重传证据，没有重跑模型 |

主数据目录共 **20 trial、22 次执行尝试**。当前 trial 为成功 16、停止 2、超时 2；所有 attempts 为成功 16、失败 1、停止 3、超时 2。当前 20 trial 均有平台接收回执。3 份评价全部为 `acceptance` 验收试填，真实人工评价 0、独立模型评价 0。独立案例数为 Sijian 3、pome 2；重复、重试和故障演练不增加独立案例数。统计快照：`.local/acceptance/final-counts.json`。

主要矩阵的 14 个真实 trial 在 Langfuse 读回 589/589 条观察、0 重复 ID、6 个配置/重复实验组、14 个条目身份匹配。恢复/故障的 7 个执行尝试另有完整读回；新增的失败重试 attempt 2 读回 71/71。平台接收与实际读回分别记录，见 `platform-business.json`、`recovery-platform-readback.json`、`revision-retry-platform.json` 及 [平台说明](../platform.md)。传输 canary 不计入业务矩阵。

## 浏览器连续旅程

MyEval 使用独立 IAB 标签实际完成下列操作。平台登录另有真实 HTTP/session 证据，未把 Langfuse 网页浏览称为已实测。

| 门槛 | 实际操作与观察 | `.local/acceptance/` 内证据 |
| --- | --- | --- |
| 建立并执行 | 空状态 → 选择项目/案例/两配置/重复/超时 → 启动 → 观察运行进度 → 结果详情 | `screenshots/01-empty.png` 至 `04-pome-trial.png`，`09-sijian-results.png` |
| 失败可定位 | 按失败筛选 → 打开真实 502 trial → 读取已交付轮次、未完成整理、模型重试和错误阶段；详情显示 HTTP 状态与接口 | `screenshots/11-failed-trial.png`；trial `cb4e7ec7-9089-40b1-b028-2f27edcdfd89` attempt 1 |
| 匿名评审 | A/B 只含可见输出；保存 unable + 理由 + 验收试填；保存后主动揭示；另一个独立席位重复读取/保存/刷新/下载 | `blind-before.json`、`screenshots/05-blind-pair.png`、`parent-review/observation.json` |
| 刷新与重启 | 刷新评价仍在；服务停启后结果、评价、A/B 顺序和内容 hash 未变；请求快照未增加；最终版本的评价 revision 同样保持 | `restart-observation.json`、`resume-restart-observation.json`、`final-restart-observation.json`、`screenshots/07-review-restored.png` |
| 比较与导出 | 项目/场景筛选 → 浏览器下载 ZIP；匿名页下载独立 JSON；离线解析附件及 hash | `export-observation.json`、`offline-export-observation.json`、`exports/` |
| 平台离线恢复 | 停止全部平台容器/VM → 真实超时试验 → MyEval 页面仍可读取和导出 → 恢复平台 → 页面重新同步本地证据 | `platform-offline.json`、`screenshots/10-platform-offline-timeout.png` |

管理员 ZIP 的 `manifest.json`、JSONL、案例和配置可独立读取；pome 场景筛选导出含 4 trial/2 次重复，Sijian 导出含 6 trial 和 6 份原生 prompt 快照。附件 hash 全部匹配。匿名导出在揭示之后仍不含配置、trial、trace 或内部结果身份。

### 重试后的过期评价修复与实操

独立母会话复核指出：旧页面如果只提交 pair ID，管理员重试后会把旧判断记到新结果上。现匿名投影携带不暴露映射的 revision；保存时在同一存储锁/事务内重新核对两侧 attempt 和 result hash。过期提交返回 HTTP 409，不新增评价，页面要求重新载入并清空旧判断。新评价在管理员归档中记录两侧真实结果身份；匿名返回继续采用白名单。

实际浏览器保持恢复实验的旧配对打开，看到 `cb4e7ec7…` attempt 1 失败；另一个管理员标签点击重试，真实 attempt 2 在 21.8 秒后成功。旧表单提交被拒绝，完成数仍为 0。重新载入后两侧为当前输出，重新填写后保存成功；从浏览器导出的 `recovery-revision.zip` 核对双方 attempt/hash 一致，仍含原失败。匿名导出没有内部身份字段。证据：`stale-review-observation.json`、`screenshots/12-stale-review-rejected.png`、`13-current-review-saved.png`、`exports/anonymous-revision-review.json`。

修复前已有的 1 份验收试填保留原记录，没有补造当时的版本字段；其已评价配对禁止重试。母会话在独立席位完成的功能试填也不计作用户偏好。

## 停止、超时与崩溃

- UI 停止实验时，`6820df81-ef8b-4116-ab15-7d87111d7f87` attempt 1 在真实模型请求后本地 cancelled；UI 明确重试形成 attempt 2 成功。两个 queued trial 为 `not_dispatched / not_sent`。同批另一请求先返回 HTTP 502，保留 failed，没有把它改写为取消。
- 两个 5 秒 trial 最终为 timed_out，协作取消和迟到证据保留。远端是否停止计算/计费未知；费用没有写成 0。
- 独立复核实际杀死 worker 后确认产品/CLI 进程组被回收；源码漂移在调用前变成显式失败；结果文件已落盘但数据库仍 running 时，崩溃重启恢复原成功，`replayed=false`、请求数 1→1。见 [独立复核](../core-review.md)。
- late cancel 增量通过真实模型请求和 OS 信号控制复现：已有终态时保留成功及停止意图；没有终态文件时为 interrupted，不能称作确认取消。两项都没有新增重复请求，原生 CLI/worker 组消失。该受控拓扑为独立 HTTP 控制进程与 runner，不能替代标准 CLI 的整个 UI 旅程；完整时序在 `.local/core-review/late-cancel/`。
- 最终停止标记读取和终态写入放在与 HTTP stop 相同的存储锁内，闭合更晚的覆盖窗口；这一极窄窗口的完整组合为代码复核范围，没有宣称穷举调度时序。

## 隔离、检查与限制

凭据源只读、pome 隔离 settings 为空对象、源资料 state hash 前后一致。初轮扫描 334 个文件未匹配执行凭据；最终对源码、运行证据和导出共 340 文件/468 个含 ZIP 成员的载荷复核，凭据匹配为 0，运行 JSON 的 reasoning_content 字段为 0，见 `final-privacy-observation.json`。Sijian `make verify-baseline` 的 56 个 V13 文件和 7 个历史评测文件 SHA 匹配。pome 用户未跟踪文档与启动前 SHA 相同，暂停声音线未恢复。匿名管理员接口隔离、源码漂移、残留进程和历史平台回执问题均已修复并复核，详见独立记录。

必要构建检查通过：`python3 -m compileall -q myeval`、接入产品变更文件的 Python 编译、分别执行 `node --check web/app.js` / `web/blind.js` / `web/view.js`、源码空白检查与集成补丁反向核对。UI 没有前端打包步骤。北京时间 13:41 用 `scripts/service stop/start` 加载最后实现；源码 SHA/修改时间、服务新 PID/启动时间与只读 API 结果保存在 `final-restart-observation.json`。所有业务表 hash、请求文件和匿名评价 revision 不变，浏览器刷新后评价仍在；管理员页面无 error/warn。语法检查不替代这些功能证据。

本首版不提供：真实用户/儿童质量结论、稳定配置优劣、真实儿童音频或设备验收、旧轨迹重评分、自动独立模型评分、供应商远端取消证明、生产权限/备份或对外部署。外部模型评价导入接口已实现并代码复核，尚无真实评分产物的完整导入旅程。refused/unsupported 终态可记录，但本轮没有专门制造供应商拒绝或穷举所有能力组合。

已观察到 pome 在原话纠正和无依据扩写上的语义问题，原结果完整保留；结构规则通过不证明质量通过。本轮未替产品修改提示词，也未用重试清除不利样本。
