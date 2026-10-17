# ADR-0002: 消息签名层延后至 v0.2 重新设计

- 状态：Accepted
- 日期：v0.1 定稿
- 关联：docs/threat-model.md、ADR-0001

## 背景

协议如实声明「sender 字段不可认证」：任何可写 bus.db 的本地进程都能伪造
任意 sender 或直接读库。一个直觉的加固方案是给每条消息附签名——
为每个实例发一把密钥（per-instance HMAC），签名校验通过即视为身份可信。

## 论证（为什么该方案在 v0.1 不成立）

per-instance HMAC 是**对称密钥**。在单机同 UID 场景下：

1. 任何总线参与进程都读得到 `~/.agent-bus/keys/*`（0600 只防其他用户）——
   「某进程读过某文件」推不出「消息来自某个特定实例」，`verified: true`
   证明力为零。
2. 按配置名误拿密钥的进程（如 `AB_AS` 误配的自动化 worker）能顺利签名
   通过——它持有的正是「它的身份」对应的密钥。
3. 消费端时效窗口（±300s）与总线的异步语义互斥：delivered 30min 超时
   重投是合法机制，窗口会把重投的合法消息误判为重放；而重放本就在
   威胁模型声明不防的范围内，窗口零收益。

结论：该密钥分发模型在单机场景**从未自洽**，术语纠错救不回设计。
带病交付一个给出虚假安全感的签名层，比诚实声明边界更糟。

## 裁决

- v0.1 只交付 `docs/threat-model.md`：如实写透边界（sender 不可认证、
  `AB_AS` 显式身份的误配风险、总线防「多任务混淆」不防恶意进程）。
- 签名硬化延后 v0.2 重新设计，方向（立项时按此评审）：
  - per-bus 共享密钥 + 独立签名表
    `message_signatures(message_id PK, alg, sig, key_fp)`（符合「minor
    只加表」纪律，历史消息天然 `verified: none`）；
  - `instance_keys(instance_id, key_fp, revoked_at)`：编号回收即吊销；
    接管旧编号记 `verified: rotated`，须重新 init；
  - 不做消费端时效窗口（理由如上）；
  - 规范序列化：`(room_id, body, sender, ts)` 按 UTF-8、`\x1f` 分隔拼接；
  - 诚实定位：「跨用户防护 + 身份卫生，**不防同 UID 进程**」；
    策略 `unverified: warn | reject | allow` 默认 warn；nonce + 消费端
    去重为可选加强。

## 后果

- v0.1 用户获得的是明确的边界陈述而非虚假的「已签名」标记；
  roadmap 公开承诺 v0.2 重设计，不静默、不空头。
- 签名的真实价值锚点在 v0.3 的跨主机 relay：签名随消息过网，
  网络对端无法读取本机密钥文件，验证才有对抗意义。
