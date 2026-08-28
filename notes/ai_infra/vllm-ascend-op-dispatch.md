# vLLM-Ascend 自定义算子下发流程

本文先说明通用设计，再以 grouped_matmul_swiglu_quant_weight_nz 为例对应源码。

# 一、整体设计

## 1. 分层

vLLM-Ascend 的自定义算子下发分为五层：

~~~text
PyTorch Dispatcher
  → 根据算子名和 DispatchKey 选择后端实现

框架适配层（vllm-ascend / torch_npu）
  → 将 PyTorch Tensor 转换为 ACL 类型
  → 编排 ACLNN 两段式调用
  → 分配 workspace，取得 stream

ACLNN Host API
  → 参数和格式检查
  → 构建 aclOpExecutor
  → 返回 workspace 需求
  → 提交 executor 执行

CANN 算子层
  → L0 算子描述任务
  → OpDef 描述接口能力
  → Host Tiling 生成执行配置
  → 构建元数据关联逻辑算子与 Kernel 二进制

AscendC Device Kernel
  → 在 AICore 上执行
~~~

PyTorch Dispatcher 负责选择 NPU 后端实现；后续调用链由 C++ 适配、ACLNN 和 CANN Runtime 承接。

## 2. ACLNN 两段式接口

ACLNN 对外暴露准备接口和执行接口：

~~~text
aclnnXXXGetWorkspaceSize(...)
  → 参数检查
  → 构建 executor
  → 计算并返回 workspaceSize

aclnnXXX(workspace, workspaceSize, executor, stream)
  → 执行 executor 中准备好的任务
~~~

调用方由 EXEC_NPU_CMD 统一编排：

~~~text
查找两个 ACLNN 动态符号
  → 调用 GetWorkspaceSize
  → torch_npu 分配 workspace
  → 调用 ACLNN 执行接口
~~~

边界要点：

- GetWorkspaceSize 同时构建 executor 并返回 workspace 大小。
- ACLNN 返回 workspace 需求，workspace 分配由 torch_npu 完成。
- aclOpExecutor 由 CANN 提供，vLLM-Ascend 在适配层使用它。
- ADD_TO_LAUNCHER_LIST_AICORE 记录任务；CommonOpExecutorRun 提交执行。

## 3. Host 与 Device 的连接方式

Host C++ 和 AscendC Kernel 使用不同编译器、指令集和地址空间，通过逻辑算子类型、构建元数据和 CANN Runtime 连接。

CANN 使用间接绑定：

~~~text
Host 提交逻辑算子类型、Tensor 和属性
  → 构建系统生成“逻辑算子 → Kernel 二进制”元数据
  → CANN Runtime 根据元数据选择 Kernel
  → Runtime 将 Kernel 和参数下发到 NPU
~~~

因此 Host 源码以逻辑算子请求表达调用关系；Host 侧 Runtime 完成 Kernel 选择，并将选定的 Device 二进制下发到 NPU。

这种分离带来：

- 同一 ACLNN 接口适配不同 SoC、dtype、format 和 Kernel 版本。
- shape、workspace、核数等动态 Tiling 决策由 CANN Host 侧统一处理。
- Kernel 算子包可独立升级，上层 PyTorch/vLLM 只依赖稳定 ACLNN ABI。
- PyTorch、MindSpore 等框架可以复用同一 ACLNN/CANN 算子实现。

代价是源码调用链不连续，需要结合注册信息和构建产物排查。

## 4. 参数如何跨过 Host/Device 边界

Host 侧向 executor 提供：

~~~text
OP_INPUT  → 输入 aclTensor
OP_OUTPUT → 输出 aclTensor
OP_ATTR   → 算子属性
~~~

CANN Runtime 在启动 Kernel 时：

- 从 aclTensor 取得 Device 地址。
- 按 OpDef 规定的顺序封装输入输出参数。
- 追加 workspace 地址。
- 追加 Host Tiling 生成的 tiling data 地址。
- 使用计算出的 blockDim 和 stream 下发任务。

OP_ATTR 通常供 Host Tiling 使用；Device 所需结果统一编码进 tiling data。

## 5. 与 CUDA 的对应关系

CUDA 的 kernel<<<...>>>() 看起来是连续调用，是因为 nvcc 隐藏了 Host stub、Device 二进制和注册过程。CANN ACLNN/OPP 模型显式分离普通 C++ Host 代码和 AscendC Device Kernel，再通过逻辑算子类型、构建元数据和 Runtime 连接。

~~~text
CUDA 常见模式：
Host wrapper → nvcc 生成的 launch stub → CUDA Runtime → Kernel

CANN ACLNN/OPP：
Host adapter → ACLNN/executor → CANN Runtime → Kernel
~~~

两者底层都由 Host Runtime 启动 Device 二进制。CUDA 由 nvcc 自动生成绑定，CANN ACLNN/OPP 由算子框架显式管理绑定。

# 二、实例：GroupedMatmulSwigluQuantWeightNZ

## 1. PyTorch 注册

位置：

~~~text
/home/yjc/project/vllm-ascend/csrc/torch_binding.cpp:2349
~~~

~~~cpp
ops.def("grouped_matmul_swiglu_quant_weight_nz(...) -> (...)");

ops.impl(
    "grouped_matmul_swiglu_quant_weight_nz",
    torch::kPrivateUse1,
    &vllm_ascend::grouped_matmul_swiglu_quant_weight_nz);
~~~

- ops.def 定义算子 schema。
- ops.impl 注册 PrivateUse1 → C++ 实现函数。
- 算子名表示 Dispatcher 中的身份；C++ 函数表示对应的具体实现。

NPU Tensor 使 Dispatcher 选中 PrivateUse1，随后进入 vllm_ascend::grouped_matmul_swiglu_quant_weight_nz()。

## 2. C++ 适配调用 EXEC_NPU_CMD

位置：

~~~text
/home/yjc/project/vllm-ascend/csrc/gmm/
grouped_matmul_swiglu_quant_weight_nz_tensor_list/
grouped_matmul_swiglu_quant_torch_adpt.h:20
~~~

适配函数创建输出 Tensor，然后调用：

~~~cpp
EXEC_NPU_CMD(
    aclnnGroupedMatmulSwigluQuantWeightNZ,
    x, weight, bias, offset,
    weight_scale, x_scale, group_list, swiglu_limit,
    output, output_scale, output_offset);
~~~

EXEC_NPU_CMD 位于：

~~~text
/home/yjc/project/torch_npu/third_party/op-plugin/
op_plugin/utils/op_api_common.h
~~~

宏根据基础名字查找：

~~~text
aclnnGroupedMatmulSwigluQuantWeightNZGetWorkspaceSize
aclnnGroupedMatmulSwigluQuantWeightNZ
~~~

它完成参数转换、第一段调用、workspace 分配、stream 获取和第二段调用。

当前 Torch 路径选择 WeightNZ 版本；普通 aclnnGroupedMatmulSwigluQuant 供其他调用路径使用。

## 3. ACLNN 第一段处理 WeightNZ

位置：

~~~text
/home/yjc/project/vllm-ascend/csrc/gmm/grouped_matmul_swiglu_quant/
op_host/op_api/aclnn_grouped_matmul_swiglu_quant.cpp
~~~

aclnnGroupedMatmulSwigluQuantWeightNZGetWorkspaceSize()：

1. 检查 weight 的 storage shape 为 5 维。
2. 将 storage format 标记为 FORMAT_FRACTAL_NZ。
3. 根据 view shape 将 view format 标记为 FRACTAL_NZ 或 ND。
4. 调用公共实现 aclnnGroupedMatmulSwigluQuantGetWorkspaceSizeCommon()。

公共实现：

~~~text
CREATE_EXECUTOR
  → 参数检查
  → 必要的 Contiguous
  → l0op::GroupedMatmulSwigluQuant
  → 输出 ViewCopy
  → executor->GetWorkspaceSize()
~~~

所以 WeightNZ 只是 ACLNN 外部入口变体。它完成 NZ 格式适配后，复用公共逻辑算子。

## 4. L0 向 executor 加入任务

位置：

~~~text
/home/yjc/project/vllm-ascend/csrc/gmm/grouped_matmul_swiglu_quant/
op_host/op_api/grouped_matmul_swiglu_quant.cpp:18
~~~

l0op 是 Level-0 Operator 命名空间，表示 ACLNN 内部靠近 Kernel 的算子构建层。

~~~cpp
OP_TYPE_REGISTER(GroupedMatmulSwigluQuant);
~~~

这声明/注册 L0 算子类型。

~~~cpp
ADD_TO_LAUNCHER_LIST_AICORE(
    GroupedMatmulSwigluQuant,
    OP_INPUT(...),
    OP_OUTPUT(...),
    OP_ATTR(...));
~~~

这把一条具体的 GroupedMatmulSwigluQuant launch 任务加入 executor，此时处于任务准备阶段。

## 5. 逻辑算子如何绑定 Device Kernel

Host 源码通过逻辑算子类型表达请求，构建阶段建立它与 grouped_matmul_swiglu_quant() 的关联：

~~~text
目录/Kernel 名：grouped_matmul_swiglu_quant
                  ↓ snake_to_camel
逻辑算子类型：   GroupedMatmulSwigluQuant
~~~

转换规则位于：

~~~text
/home/yjc/project/vllm-ascend/csrc/cmake/gen_ops_info.cmake:202
~~~

相关注册和构建输入：

~~~text
OP_ADD(GroupedMatmulSwigluQuant)
  → OpDef：输入输出、dtype、format、SoC

IMPL_OP_OPTILING(GroupedMatmulSwigluQuant)
  → Host Tiling 回调

op_kernel/grouped_matmul_swiglu_quant.cpp
  → AscendC Device Kernel
~~~

构建系统生成 Kernel 二进制和 binary_info_config.json。常见位置：

~~~text
csrc/build/binary/<soc>/bin/config/<soc>/binary_info_config.json

vllm_ascend/_cann_ops_custom/vendors/custom_transformer/
op_impl/ai_core/tbe/kernel/config/<soc>/binary_info_config.json
~~~

Runtime 使用逻辑算子类型和这些元数据找到对应 Kernel 二进制并完成启动。

## 6. 第二段提交执行

WeightNZ 第二段接口位于：

~~~text
aclnn_grouped_matmul_swiglu_quant.cpp:529
~~~

~~~cpp
aclnnStatus aclnnGroupedMatmulSwigluQuantWeightNZ(
    void *workspace,
    uint64_t workspaceSize,
    aclOpExecutor *executor,
    aclrtStream stream)
{
    return CommonOpExecutorRun(
        workspace, workspaceSize, executor, stream);
}
~~~

CommonOpExecutorRun 声明来自：

~~~cpp
#include "opdev/op_executor.h"
~~~

这里才把准备好的 executor 提交给 CANN Runtime。

## 7. 本例的参数映射

L0 通过 OP_INPUT、OP_OUTPUT 和 OP_ATTR 保存参数：

~~~text
OP_INPUT(x)                       → GM_ADDR x
OP_INPUT(weight)                  → GM_ADDR weight
OP_INPUT(perChannelScale)         → GM_ADDR weightScale
OP_INPUT(perTokenScale)           → GM_ADDR xScale
OP_INPUT(weightAssistanceMatrix)  → GM_ADDR weightAssistanceMatrix
OP_INPUT(groupList)               → GM_ADDR groupList
OP_OUTPUT(out)                    → GM_ADDR y
OP_OUTPUT(scaleOut)               → GM_ADDR yScale
~~~

CANN 另外补充：

~~~text
EXEC_NPU_CMD 分配的 workspace → GM_ADDR workspace
Host Tiling 生成的数据        → GM_ADDR tiling
~~~

最终 Device 入口：

~~~text
/home/yjc/project/vllm-ascend/csrc/gmm/grouped_matmul_swiglu_quant/
op_kernel/grouped_matmul_swiglu_quant.cpp:64
~~~

~~~cpp
__global__ __aicore__ void grouped_matmul_swiglu_quant(
    GM_ADDR x,
    GM_ADDR weight,
    GM_ADDR weightScale,
    GM_ADDR xScale,
    GM_ADDR weightAssistanceMatrix,
    GM_ADDR groupList,
    GM_ADDR y,
    GM_ADDR yScale,
    GM_ADDR workspace,
    GM_ADDR tiling);
~~~

## 8. Tiling 和具体分支

Tiling 注册：

~~~text
/home/yjc/project/vllm-ascend/csrc/gmm/grouped_matmul_swiglu_quant/
op_host/grouped_matmul_swiglu_quant_tiling.cpp:285
~~~

Tiling 在 Host 侧根据 shape、dtype、format、SoC 等产生 tiling key、tiling data、blockDim 和 workspace 需求。

Device Kernel 根据 tiling key 进入实现：

~~~text
key 0 → GMMSwigluCompute
key 1 → GMMSwigluSplitWorkSpaceCompute
key 2 → GMMSwigluQuantPipelineSchedule（A8W4 MSD）
~~~

## 9. 本例最容易误解的三点

1. WeightNZ 是 ACLNN 外部入口变体；Device 执行路径复用 l0op::GroupedMatmulSwigluQuant、同一 Tiling 和 grouped_matmul_swiglu_quant Kernel。
2. op_host/op_api/grouped_matmul_swiglu_quant.cpp 承担 L0 任务构建；op_kernel/grouped_matmul_swiglu_quant.cpp 定义 Device Kernel。
3. 自定义算子可通过 ACLNN 实现、OP_ADD、IMPL_OP_OPTILING 和 __aicore__ Kernel 四类源码共同确认。本例四者均位于 vllm-ascend。运行时还可检查 ACLNN 符号来自 libcust_opapi.so 或内置 libopapi.so。
