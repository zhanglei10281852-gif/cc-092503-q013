# 科研样品全生命周期管理服务

这是一个面向科研机构样品库、实验室和课题组的模块化后端，集中管理样品接收、分装、借用、归还、消耗、销毁、库存盘点、谱系事件、保管位置、异常记录、登录权限、审计以及可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 已有能力

- 身份与权限：支持引导管理员、登录、会话、用户、角色和细粒度权限。
- 批次与二维码：接收批次保存项目、数量和稳定二维码载荷。
- 样品档案：登记样品、数量、单位、保管位置和生命周期状态。
- 分装谱系：一次事务内扣减母样、创建子样、记录损耗与事件链。
- 借用归还：保存借用数量、到期时间、部分归还和最终归还状态。
- 实验消耗：使用幂等键登记消耗，防止重复请求二次扣减。
- 双向交接：位置转移改为发起冻结与接收验收两段式，验收齐全后原子过户，拒收或超时退回源库并生成异常。
- 位置脱敏：普通权限只能看到受限位置的替代码，授权人员可查看精确位置。
- 双人审批：高风险操作要求申请人与审批人分离，并累计不同审批人的决定。
- 异常追踪：异常可以关联样品或接收批次，保存严重度和处理状态。
- 审计与任务：关键身份及业务操作留痕，后台任务支持去重、领取与完成。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/samples.db`，可用 `SAMPLE_DATABASE_PATH` 指定其他路径。

## 初始化与完整性检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
python -m pytest
```

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

## 位置双向交接说明

- 发起：`POST /api/sample-operations/{sample_id}/handovers` 冻结样品全部数量（`reserved_quantity` 顶满）并记录源位置版本、样品版本与清单摘要（manifest_digest），样品在全局查询中仍位于源位置并标记 `active_transfers`。
- 验收：`POST /api/sample-operations/handovers/{id}/receipts` 由非发起人逐项确认数量、封签与目标位置，`receive_token` 幂等防重复扫描；全部明细确认后同一事务内完成位置、保管人与解冻变更。
- 终止：`cancel`（仅发起人、且无验收记录）、`reject`（接收方，生成异常并退回）、超时（默认 48 小时，任何交接接口触发惰性结算，也可由 `POST /api/sample-operations/handovers/sweep-expired` 定时清理）。所有终态转换带状态守卫，并发或重试只会得到一个终态。
- 追责：交接全程事件以 `transfer_code` 为 correlation_id 写入样品事件链，交接单详情返回事件、明细与验收记录。
