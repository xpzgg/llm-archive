



1. 编译时
  native_functions.yaml
    → torchgen 读取 Schema、dispatch、structured 等信息
    → 生成 m.def / m.impl 注册代码
    → 生成 add_Tensor API 和 Kernel wrapper
2. PyTorch 加载时
  m.def()  → 把 Schema 注册进全局 Dispatcher
    m.impl() → 把 DispatchKey → Kernel 注册进同一算子的调度表
3. 算子调用时
  add_Tensor::call()
    → 根据算子名 + 重载名找到它的 Handle
    → 从输入 Tensor 得到 DispatchKeySet
    → 从中选择优先级最高的 DispatchKey
    → 查询该算子的调度表
    → 找到对应 Kernel 并调用



# 算子定义

```yaml
// native_functions.yaml

- func: add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor
  device_check: NoCheck   # TensorIterator
  structured_delegate: add.out
  variants: function, method
  dispatch:
    SparseCPU, SparseCUDA, SparseMPS, SparseMeta, SparseXPU: add_sparse
    SparseCsrCPU, SparseCsrCUDA, SparseCsrMeta, SparseCsrXPU: add_sparse_csr
    MkldnnCPU: mkldnn_add
    ZeroTensor: add_zerotensor
    NestedTensorCPU, NestedTensorHPU, NestedTensorCUDA, NestedTensorXPU: NestedTensor_add_Tensor
  tags: [core, pointwise]
  
 - func: add.out(Tensor self, Tensor other, *, Scalar alpha=1, Tensor(a!) out) -> Tensor(a!)
  device_check: NoCheck   # TensorIterator
  structured: True // 同一个 meta 加各后端的 impl，自动拼装出 functional、inplace、out 三种接口。
  structured_inherits: TensorIteratorBase
  ufunc_inner_loop: // universal function, 逐元素操作
    Generic: add (AllAndComplex, BFloat16, Half, ComplexHalf)
    ScalarOnly: add (Bool)
  dispatch:
    SparseCPU, SparseMeta: add_out_sparse_cpu
    SparseCUDA: add_out_sparse_cuda
    SparseMPS: add_out_sparse_mps
    SparseXPU: add_out_sparse_xpu
    SparseCsrCPU, SparseCsrMeta: add_out_sparse_compressed_cpu
    SparseCsrCUDA: add_out_sparse_compressed_cuda
    SparseCsrXPU: add_out_sparse_compressed_xpu
    MkldnnCPU: mkldnn_add_out
    MPS: add_out_mps
    MTIA: add_out_mtia
    XPU: add_out_xpu
  tags: pointwise
```





# 算子注册

## 注册schema

```c++
// build/aten/src/ATen/RegisterSchema.cpp
// 注册算子的身份和接口
m.def("add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor", tags_7);

m.def
→ Library::_def
→ Dispatcher::registerDef
 
```



## 注册kernel

```c++
 // build/aten/src/ATen/RegisterCUDA_0.cpp
// 某个 DispatchKey 下由谁实现
TORCH_LIBRARY_IMPL(aten, CUDA, m) {
    m.impl("add.Tensor", TORCH_FN(wrapper_CUDA_add_Tensor)); 
m.impl("add.out", TORCH_FN(wrapper_CUDA_add_out_out));   
m.impl("add_.Tensor", TORCH_FN(wrapper_CUDA_add__Tensor));
}

m.impl
→ Library::_impl
→ Dispatcher::registerImpl
```



沿着 `RegisterSchema.cpp` 的 `m.def()`，看它怎样创建这个 `OperatorEntry`

```
// build/aten/src/ATen/RegisterSchema.cpp
m.def("add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor", tags_7);

m.def(...)
  ↓
Library::def(...)
  ↓
Library::_def(...)
  ↓
Dispatcher::registerDef(...)
  ↓
findOrRegisterName_("aten::add.Tensor")
  ↓
创建 OperatorEntry
  ↓
OperatorEntry::registerSchema(...)

c10::Dispatcher::singleton()


Dispatcher
└── operatorLookupTable_
    └── "aten::add.Tensor"
        └── OperatorEntry
            ├── name_
            ├── schema_
            ├── kernels_
            └── dispatchTable_
```





# 算子调用





```
torch.add(a, b)

// torch/csrc/autograd/generated/python_torch_functions_2.cpp
“add", castPyCFunctionWithKeywords(THPVariable_add), METH_VARARGS | METH_KEYWORDS | METH_STATIC, nullptr},


THPVariable_add // pyobject, c++ tensor互转

```



  

## Tensor API



```c++
inline at::Tensor Tensor::add(
    const at::Tensor& other,
    const at::Scalar& alpha) const {

    return at::_ops::add_Tensor::call(
        const_cast<Tensor&>(*this),
        other,
        alpha
    );
}
```



## Dispatcher 入口

```c++
// 1. torchgen 读取并解析 Schema，
// 生成 build/aten/src/ATen/ops/add_ops.h

// torch codegen 生成的算子 dispatcher 入口
struct TORCH_API add_Tensor {
  using schema = at::Tensor (const at::Tensor &, const at::Tensor &, const at::Scalar &);
  using ptr_schema = schema*;
  // See Note [static constexpr char* members for windows NVCC]
  static constexpr const char* name = "aten::add";
  static constexpr const char* overload_name = "Tensor";
  static constexpr const char* schema_str = "add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor";
  static at::Tensor call(const at::Tensor & self, const at::Tensor & other, const at::Scalar & alpha);
  static at::Tensor redispatch(c10::DispatchKeySet dispatchKeySet, const at::Tensor & self, const at::Tensor & other, const at::Scalar & alpha);
};
```



```cpp
// build/aten/src/ATen/Operators_2.cpp

// aten::add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor
static C10_NOINLINE c10::TypedOperatorHandle<add_Tensor::schema> create_add_Tensor_typed_handle() {
  return c10::Dispatcher::singleton() // 拿到全局 Dispatcher
      .findSchemaOrThrow(add_Tensor::name, add_Tensor::overload_name) // OperatorHandle->OperatorEntry
      .typed<add_Tensor::schema>();
}

// aten::add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor
at::Tensor add_Tensor::call(const at::Tensor & self, const at::Tensor & other, const at::Scalar & alpha) {

    static auto op = create_add_Tensor_typed_handle();
    return op.call(self, other, alpha); // 通过调度表和DispatchKey，找到kernel wrapper
}
```



## Wrapper



```
add.Tensor   wrapper ─┐
add_.Tensor  wrapper ─┼─→ 同一套 meta + impl
add.out      wrapper ─┘


at::Tensor wrapper_CUDA_add_Tensor(const at::Tensor & self, const at::Tensor & other, const at::Scalar & alpha) {
  // No device check
structured_ufunc_add_CUDA_functional op;
op.meta(self, other, alpha);  // shape推导，创建输出 tensor
op.impl(self, other, alpha, op.outputs_[0]); // 进入 CUDA 计算和下发路径
return std::move(op.outputs_[0]);
}
```



## impl



```
// op.impl
// build/aten/src/ATen/UfuncCUDA_add.cu
TORCH_IMPL_FUNC(ufunc_add_CUDA)(const at::Tensor & self, const at::Tensor & other, const at::Scalar & alpha, const at::Tensor & out) {
  add_kernel(*this, alpha);
}
```



## 选择kernel 模板

根据 dtype 选择 `float`、`half`、`double` 等具体模板。

```
void add_kernel(
    TensorIteratorBase& iter,
    const at::Scalar& alpha) {

    AT_DISPATCH_SWITCH(
        iter.common_dtype(),
        "ufunc_add_CUDA",
        ...
        gpu_kernel(
            iter,
            CUDAFunctor_add<scalar_t>(...) // 逐元素计算func
        );
    );
}
```



## GPU kernel

```
dim3 block(nt);
dim3 grid((N + block.x * vt - 1) / (block.x * vt));

auto stream = at::cuda::getCurrentCUDAStream();

elementwise_kernel<<<grid, block, 0, stream>>>(
    N,
    f
);
```

