# 实现算力券核销与联合资助分摊基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，以及区域人工智能扶持计划下的算力券核销与联合资助分摊。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/voucher_fund/`：算力券批次登记、资助规则版本化、作业确认分摊与冻结、按实际消耗核销、双人复核人工调整；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 算力券核销与联合资助分摊

- 资助方登记券批次：适用租户、资源范围、有效期和资助上限，市级券与园区补贴分层管理；
- 运营为租户发布版本化资助规则（核销顺序与各层分摊比例上限，企业自付兜底）；规则修订生成新版本，不改写已确认作业；
- 作业确认时按规则生成可解释的分摊方案（每行注明依据，额度不足的下落部分有说明），并冻结对应批次额度；
- 作业完成按实际消耗在核销顺序内核销；取消与失败释放全部冻结额度，部分完成核销已消耗部分并结转未用额度；
- 人工调整分摊方案需第二名运营复核通过，生效后形成新的方案版本，冻结额度随版本原子切换；
- 提交、确认与核销请求均按幂等键安全重放，同一编号对应不同内容必须冲突；
- 租户仅见本企业作业明细，资助方仅见本资助方批次的分摊与核销明细，审计人员可核对全量明细与哈希链审计日志。

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
PYTHONPATH=src python3 -m voucher_fund.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和算力券核销流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m voucher_fund.api --database voucher.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
