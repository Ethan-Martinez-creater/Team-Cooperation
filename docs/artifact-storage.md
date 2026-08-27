# 不可变制品内容存储

`ArtifactContentService` 将现有 PostgreSQL manifest 注册表与 `ArtifactObjectStore` 组合。上传按流计算
SHA-256 和长度，只在声明身份完全一致时提交不可变对象，然后发布绑定 `artifact-store://tenant/digest`
的 manifest。下载必须先通过 manifest 的租户可见性、clearance、compartment 和期望摘要检查，再读取对象；
本地后端在每次读取时重新验证完整内容摘要，并支持受边界约束的 range。

`LocalImmutableArtifactStore` 用于单机和受控私有部署：根目录必须预先创建且不能是符号链接，租户和摘要
只能形成规范路径，目录链不能经过符号链接；同摘要上传会重新校验已有对象，冲突或磁盘篡改默认拒绝。
上传使用同目录临时文件、`fsync` 和原子 hard-link 提交，失败临时文件按明确路径清除。

多节点生产部署应实现同一 `ArtifactObjectStore` 协议，使用 S3/兼容对象存储、Azure Blob 或 GCS 的
immutable/version-lock、服务端加密、私有 endpoint 和工作负载身份。实现必须保持：单租户 namespace、
内容寻址、条件创建、流式摘要、长度门禁、幂等提交、range 边界和无预签名 URL/凭据进入 manifest。
对象存储成功而数据库事务失败时允许留下未引用对象；后台回收只能依据保留策略和引用快照处理，不能在
请求失败路径直接删除可能已被并发引用的内容。
