# 实现算力券核销与联合资助分摊基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景、硬件稳定性准入与算力券联合资助核销。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险、资助方运营和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/voucher_settlement/`：算力券批次登记、联合资助分摊、额度冻结/核销/释放/结转、规则版本与双人复核调整；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m voucher_settlement.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和算力券联合资助核销流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m voucher_settlement.api --database voucher.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON 并通过 `X-Actor-Id` 标识操作者。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 算力券核销与联合资助分摊

`voucher_settlement` 包实现：

- **券批次登记**：资助方、适用租户、资源范围（产品/设施/不限）、有效期、总资助上限、单作业上限与覆盖比例；
- **联合资助分摊**：作业确认时按优先级生成可解释方案（每条份额附带约束说明），依次受覆盖比例、单作业上限和批次剩余额度约束，余额自动转企业自付，并冻结对应额度；
- **核销**：作业完成按实际消耗核销；失败全额释放；部分完成按实际核销、余额结转给该租户（结转优先于批次预算使用，90 天有效，过期自动释放回批次池）；
- **规则版本**：资助规则修订追加不可变新版本，已确认作业保留确认时的规则快照，资助上限不得低于已冻结与已核销之和；
- **人工调整**：调整申请必须由第二人复核，批准后追加结算新版本，驳回不影响原结算；
- **幂等**：确认、结算、取消、调整均带幂等键，相同请求安全重放，不同内容复用键返回 409；
- **权限视图**：租户只见本租户与适用规则、资助方只见本资助方份额（企业自付与其他资助方脱敏）、运营/复核/审计见全量；
- **审计**：全部状态变化写入 SHA-256 哈希链，可通过 `GET /audit/chain` 校验完整性。
