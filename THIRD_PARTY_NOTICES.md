# 第三方说明

MyEval 核心依赖 Python 标准库；前端未打包第三方框架、字体或图标库。

`infra/compose.yaml` 派生自 Langfuse 4.76.0 的官方 Compose 配置，用于可选的本机观测服务。原许可声明保留在 [infra/LICENSE.langfuse](infra/LICENSE.langfuse)。容器镜像按 digest 引用；镜像和其内组件不随本仓库分发，各自适用原许可。

Sijian、pomegranagent-v2 与 Sidelit 的完整产品代码、模型权重和运行环境不包含在本仓库。`integrations/` 是历史接入补丁，不能视为对应产品或完整 V2 的发行包。

Sidelit 任务包引用的公开图片在各案例的 `source` 字段中保存来源、许可和署名；原图不随本仓库分发。README 和操作指南中的截图来自本仓库的虚构办公演示，没有使用真实用户或真实模型输出。

这些说明不为第三方组件重新授予许可，也不替代其原许可条款。
