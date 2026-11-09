# 母会话独立复核

2026-11-09，由“思间 · 主脑”在 MyEval 实现主脑之外完成。此记录区分源码复核、母会话实际操作和实现主脑的验收证据，不代表用户质量评审。

## 发现并关闭的问题

独立只读复核发现：盲评页面打开 attempt 1 后，管理员可以在尚未保存评价时重试；旧页面提交未携带结果身份，可能把旧判断绑定到 attempt 2。实现主脑已增加匿名 presentation revision，将两侧 trial、attempt、状态与结果哈希绑定；保存时在同一锁和事务内核对，过期返回 `409 stale_presentation`。评价归档保留结果身份，匿名投影不返回内部身份字段。二次源码复核未发现该窗口的剩余缺口。

实现主脑实际保持旧盲评页面打开，对原 HTTP 502 失败 trial 做一次真实重试，再从旧页面提交。页面明确提示结果/页面版本已变化且评价未保存；观察文件显示 completed=0、review=null、revision 已改变，随后重新载入才可评价。证据：`.local/acceptance/stale-review-observation.json` 及对应截图、`exports/recovery-revision.zip`。母会话读取了这些记录；该特定重试操作由实现主脑执行。

## 母会话实际浏览器旅程

在独立 IAB 标签及“母会话独立验收试填”席位上，使用已有 pome 实验 `60cb6073-8797-4fd1-85fc-488e0f6694c4` 的虚构材料：

1. 阅读完整儿童原话、期望边界、两侧实际三轮交付及整理结果。
2. 选择“无法判断”和“验收试填”，填写明确的功能验收说明后保存，页面从 0/4 变为 1/4。
3. 刷新后评价、理由和“验收试填”标记保留；A/B 内容保持。
4. 保存后主动揭示配置，再从实际下载按钮导出匿名 JSON。导出包含评价和 presentation revision，不含配置、trial、result_identity、模型或 trace/run 映射。

主脑最终服务重启（05:41:08 UTC）后，母会话再次实际刷新同一页面，评价仍显示 1/4 与“已保存 · 无法判断”；最终截图为 `anonymous-final.jpg`，页面错误/警告日志为空。

证据位于 `.local/acceptance/parent-review/`：`observation.json`、浏览器实际下载的 `anonymous-before.json` / `anonymous-after.json`、`anonymous-before.jpg` / `anonymous-saved.jpg`。这条记录不计作真实人工偏好，母会话没有为了本次旅程触发新模型请求。

## 其他只读核对

- 已有三个管理员 ZIP 的附件存在且哈希匹配；重试 trial 的两次 request/result/events 可进入导出附件。独立复核时数据库为 20 trial / 21 attempt，之后主脑的增量重试另计，最终数量见首版报告。
- 已保存评价与重试在同一锁内互斥；旧 attempt 保留。匿名接口使用白名单，管理员接口独立认证。
- 两个接入仓库 HEAD 与启动前相同；工作树变更位于已约定的思间桥接和 pome 观测接口。pome 用户未跟踪文件 `docs/development-retrospective.md` 的 SHA-256 与启动前一致。

未由母会话重复执行整套模型矩阵、平台部署或全部取消/崩溃组合。上述证据不证明真实儿童语音、真机体验、模型普遍质量、真实用户偏好或生产部署能力；首版综合范围与限制由 `first-release.md` 汇总。
