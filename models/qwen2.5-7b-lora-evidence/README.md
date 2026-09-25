# 船舶航速优化模型训练产物

本目录保存第一次智能体训练生成的 Qwen2.5-7B-Instruct LoRA 适配器及对应配置、训练结果，用于展示模型训练产物。它是独立的模型材料，不属于当前在线版的调用链。

## 文件

| 文件 | 用途 |
| --- | --- |
| `adapter_model.safetensors` | LoRA 微调权重 |
| `adapter_config.json` | 基座模型标识及 LoRA 结构参数 |
| `train_results.json` | 训练过程结果 |
| `eval_results.json` | 验证集结果 |

适配器配置记录的基座为 **Qwen2.5-7B-Instruct**，LoRA rank 为 16、alpha 为 32，作用于 `q_proj` 和 `v_proj`。训练结果文件记录了 3 个 epoch 和验证损失。以上信息来自随附配置及训练输出文件。

## 基座模型

上游模型：[Qwen/Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)。请按上游页面的许可与获取说明使用基座模型。此仓库只保存本项目生成的 LoRA 适配器，不重新分发完整基座权重。

第一次训练目录中另有量化文件 `ship-qwen-q4.gguf`，大小为 4,683,073,408 字节（约 4.36 GiB），SHA-256 为：

```text
ee2cbe20c598b7ff14e5f1b50a550b6c31a7a843fea45cd78355fdee5b4968ff
```

该 GGUF 文件留存在训练设备中，未上传至本仓库。GitHub Free 的 Git LFS 单文件上限为 2 GiB；仓库保留模型来源、文件大小与校验值，供核对本地文件。

## 适配器校验

`adapter_model.safetensors` 的 SHA-256：

```text
E338B68F2A886DCA548BC11E3CCB1093781F90A2038139DA53C9B352DDB73E36
```

要运行该适配器，需要另行获取匹配的 Qwen2.5-7B-Instruct 基座，并使用 PEFT 等兼容工具加载。此目录用于记录模型训练产物，不构成航行安全或实船验证结论。
