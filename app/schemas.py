"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

PositiveInt = Annotated[int, Field(ge=1)]

# 对外稳定的瑕疵类型枚举（中文契约）
DefectType = Literal["模糊", "划痕", "曝光异常"]


class BatchIn(BaseModel):
    roll_no: str = Field(..., min_length=1, description="胶卷编号")
    expected_start: int = Field(..., ge=0, description="预期帧号起始（含）")
    expected_end: int = Field(..., ge=0, description="预期帧号结束（含）")
    frames: list[int] = Field(..., min_length=1, description="本批扫描帧号列表")
    batch_id: str | None = Field(
        None, description="客户端批次号（可选）；留空则按内容生成"
    )

    @field_validator("roll_no", "batch_id")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("expected_end")
    @classmethod
    def _check_range(cls, v: int, info) -> int:
        start = info.data.get("expected_start")
        if start is not None and v < start:
            raise ValueError("expected_end 不能小于 expected_start")
        return v


class BatchResult(BaseModel):
    batch_id: str
    roll_no: str
    repeated: bool = Field(..., description="True=内容相同的批次已导入过，本次未做任何更改")
    added_frames: list[int]
    duplicates: list[int] = Field(..., description="重复帧（批内重复 + 胶卷已有），只计一次")
    merged_frames: list[int] = Field(..., description="导入后该胶卷的全部帧号（去重排序）")
    expected_ranges: list[list[int]]
    missing_ranges: list[list[int]] = Field(..., description="预期范围内的连续缺帧区间")
    out_of_range_frames: list[int] = Field(..., description="落在全部预期范围之外的帧")
    created_at: str | None = None


class BatchPrecheckOut(BaseModel):
    """预检单：相对创建时胶卷状态的预演结果（此时尚未写入任何批次）。"""

    precheck_id: str = Field(..., description="预检单 ID，凭此确认导入")
    roll_no: str
    batch_id: str | None = Field(
        ..., description="预检提交时携带的客户端批次号；留空时确认后服务端自动生成"
    )
    expected_start: int = Field(..., description="本批预期帧号起始（含）")
    expected_end: int = Field(..., description="本批预期帧号结束（含）")
    raw_frames: list[int] = Field(
        ..., description="提交的原始帧列表（含批内重复，保留提交时的原始顺序）"
    )
    added_frames: list[int] = Field(
        ..., description="若此刻导入将新增的帧号（去重排序）"
    )
    duplicates: list[int] = Field(
        ..., description="重复帧（批内重复 + 胶卷已有），只计一次"
    )
    current_expected_ranges: list[list[int]] = Field(
        ..., description="创建预检单时胶卷已有的合并预期覆盖闭区间"
    )
    post_import_expected_ranges: list[list[int]] = Field(
        ..., description="导入后胶卷的合并预期覆盖闭区间"
    )
    post_import_missing_ranges: list[list[int]] = Field(
        ..., description="导入后预期范围内的连续缺帧区间"
    )
    state_fingerprint: str = Field(
        ...,
        description="创建时胶卷状态（预期范围 + 实扫帧集合）的 SHA-256 指纹，供展示与防御性复核",
    )
    batch_revision: int | None = Field(
        ...,
        description="创建时胶卷持久化、单调递增的批次修订号（批次实际导入/撤销才递增，"
        "内容幂等重试不递增）；确认时在同一写事务内复核，不一致即 409。"
        "旧库升级前创建的预检单为 null：未确认者确认时一律 409 要求重新预检，"
        "已确认者仍返回首次冻结结果。",
    )
    confirmed: bool = Field(..., description="是否已确认导入")
    created_at: str
    confirmed_at: str | None = None


class BatchPrecheckConfirmOut(BatchResult):
    """确认预检单的结果：与 POST /batches 响应同构，另带预检单信息。

    同一预检单再次确认原样返回首次结果（import_status_code 也与首次一致）。
    """

    precheck_id: str
    confirmed: bool = Field(
        ..., description="True=本次为重复确认，返回的是首次导入冻结的结果"
    )
    import_status_code: int = Field(
        ..., description="首次导入对应的状态码：201 新建 / 200 内容重复"
    )


class QualityAlertThresholds(BaseModel):
    duplicate_rate: float = Field(
        ..., description="批内重复率告警阈值（达到即触发 HIGH_DUPLICATE_RATE）"
    )
    gap_length: int = Field(
        ..., description="预期内最长缺口告警阈值（帧数，达到即触发 LONG_MISSING_GAP）"
    )


class BatchQualityOut(BaseModel):
    batch_id: str
    roll_no: str
    expected_start: int = Field(..., description="本批预期帧号起始（含）")
    expected_end: int = Field(..., description="本批预期帧号结束（含）")
    frame_count: int = Field(..., description="原始帧列表长度（含批内重复）")
    unique_frame_count: int = Field(..., description="去重后的帧数")
    duplicate_rate: float = Field(
        ...,
        description="批内重复率 = (frame_count − unique_frame_count) / frame_count，保留 4 位小数",
    )
    out_of_range_frames: list[int] = Field(
        ..., description="落在本批预期闭区间之外的帧（去重排序）"
    )
    longest_scanned_run: list[int] | None = Field(
        ...,
        description="按原始顺序压缩出的最长连续扫到段 [start, end]（闭区间，并列取先出现者）",
    )
    longest_scanned_run_length: int = Field(..., description="最长连续扫到段长度（帧数）")
    longest_gap: list[int] | None = Field(
        ..., description="预期闭区间内的最长缺帧段 [start, end]（并列取帧号最小者）；无缺口为 null"
    )
    longest_gap_length: int = Field(..., description="预期闭区间内最长缺口长度（帧数）")
    segment_count: int = Field(
        ..., description="原始帧列表按原始顺序压缩成连续段后的段数"
    )
    alerts: list[str] = Field(
        ...,
        description="稳定告警代码，按阈值触发：HIGH_DUPLICATE_RATE（批内重复率）、"
        "FRAMES_OUT_OF_RANGE（越界帧）、LONG_MISSING_GAP（预期内缺口长度）",
    )
    alert_thresholds: QualityAlertThresholds = Field(
        ..., description="本次评估使用的告警阈值"
    )
    created_at: str = Field(..., description="批次导入时间（UTC ISO-8601）")


# ---------- 批次帧号整体偏移诊断 ----------

class OffsetSuggestionOut(BaseModel):
    """批次帧号整体偏移诊断结果：仅当最优非零偏移严格优于偏移 0 时给出建议。"""

    batch_id: str
    roll_no: str
    max_shift: int = Field(..., description="本次诊断允许的最大偏移绝对值（帧数）")
    suggested: bool = Field(
        ..., description="是否给出非零整体偏移建议（最优非零偏移严格优于偏移 0 时才为 true）"
    )
    offset: int | None = Field(
        ..., description="建议的整体偏移量：该批帧号加上 offset 后重排；无建议时为 null"
    )
    original_covered_count: int = Field(
        ..., description="偏移 0（保持现状）时该批去重帧落入基线缺帧的不同帧数"
    )
    shifted_covered_count: int | None = Field(
        ..., description="按建议偏移平移后落入基线缺帧的不同帧数；无建议时为 null"
    )
    collision_count: int | None = Field(
        ..., description="按建议偏移平移后撞上其余存活批次实扫帧的不同帧数；无建议时为 null"
    )


# ---------- 扫描瑕疵复核 ----------

class DefectIn(BaseModel):
    position: int = Field(
        ...,
        ge=0,
        description="瑕疵帧在该批**原始帧列表**中的位置（0 起算）",
    )
    defect_type: DefectType = Field(
        ..., description="瑕疵类型：模糊 / 划痕 / 曝光异常"
    )


class BatchDefectsIn(BaseModel):
    defects: list[DefectIn] = Field(
        ..., description="该批全部瑕疵标记，按位置 0 起算；PUT 整体替换（含清空）"
    )

    @field_validator("defects")
    @classmethod
    def _no_duplicate_positions(cls, v: list[DefectIn]) -> list[DefectIn]:
        seen: set[int] = set()
        for item in v:
            if item.position in seen:
                raise ValueError("position 不能重复")
            seen.add(item.position)
        return v


class DefectOut(BaseModel):
    position: int = Field(..., description="在该批原始帧列表中的位置（0 起算）")
    frame_no: int = Field(..., description="该位置对应的扫描帧号")
    defect_type: DefectType


class BatchDefectsOut(BaseModel):
    batch_id: str
    roll_no: str
    frame_count: int = Field(
        ..., description="该批原始帧列表长度（合法位置为 0 … frame_count−1）"
    )
    defects: list[DefectOut] = Field(
        ..., description="当前生效的全部瑕疵标记，按位置升序"
    )


class DefectQueueItem(BaseModel):
    frame_no: int = Field(..., description="入队帧号")
    defect_occurrence_count: int = Field(
        ...,
        description="瑕疵出现次数：该帧在当前胶卷存活批次原始列表中的总出现次数"
        "（入队即全部出现均已被标记瑕疵，故此计数等于已标记出现次数）",
    )


class DefectQueueOut(BaseModel):
    roll_no: str
    frames: list[DefectQueueItem] = Field(
        ...,
        description="所有有效扫描出现均被标记瑕疵的帧，按帧号升序；"
        "同帧任一未标记出现（未提交瑕疵的批次、旧批次去重帧）即不入队",
    )


# ---------- 胶卷交付核验 ----------

class DeliveryCheckBatch(BaseModel):
    batch_id: str = Field(..., description="签发时存活批次的身份（按原导入顺序）")
    content_hash: str = Field(..., description="该批次内容指纹（预期范围 + 帧号集合）")


class DeliveryCheckDefect(BaseModel):
    batch_id: str = Field(..., description="瑕疵标记所在批次")
    position: int = Field(..., description="瑕疵在该批原始帧列表中的位置（0 起算）")
    frame_no: int = Field(..., description="该位置对应的扫描帧号")
    defect_type: DefectType


class DeliveryCheckOut(BaseModel):
    """交付核验结果：未签发时仅返回三类阻断项；签发成功/同状态重放时携带凭证。"""

    delivery_check_id: str | None = Field(
        ..., description="凭证 ID；存在阻断项未签发时为 null"
    )
    roll_no: str
    issued: bool = Field(
        ..., description="是否签发持久化凭证（三类阻断项全空才为 true）"
    )
    repeated: bool = Field(
        ..., description="True=同一状态重复申请，返回已签发的原凭证而非新建"
    )
    missing_ranges: list[list[int]] = Field(
        ..., description="阻断项一：预期覆盖内的连续缺帧闭区间"
    )
    out_of_range_frames: list[int] = Field(
        ..., description="阻断项二：落在全部预期覆盖之外的实扫帧（去重排序）"
    )
    defect_frames: list[int] = Field(
        ...,
        description="阻断项三：全部有效扫描出现均被标记瑕疵的帧号（按帧号升序）",
    )
    expected_ranges: list[list[int]] | None = Field(
        ..., description="签发/重放时冻结的预期覆盖闭区间；未签发时为 null"
    )
    frames: list[int] | None = Field(
        ..., description="签发/重放时冻结的实扫帧（去重排序）；未签发时为 null"
    )
    batches: list[DeliveryCheckBatch] | None = Field(
        ..., description="签发/重放时冻结的存活批次身份；未签发时为 null"
    )
    defects: list[DeliveryCheckDefect] | None = Field(
        ...,
        description="签发/重放时冻结的全部瑕疵标记（批次身份、0 起算位置与类型）；"
        "未签发时为 null",
    )
    state_fingerprint: str | None = Field(
        ...,
        description="签发/重放时冻结的状态指纹（存活批次身份 + 瑕疵位置与类型的"
        "SHA-256）；未签发时为 null",
    )
    created_at: str | None = Field(
        ..., description="凭证签发时间（UTC ISO-8601）；未签发时为 null"
    )


class DeliveryCheckDetailOut(BaseModel):
    """凭证查询：返回冻结的签发依据，并按当前批次与瑕疵标记重算有效性。"""

    delivery_check_id: str
    roll_no: str
    currently_valid: bool = Field(
        ...,
        description="当前状态指纹是否与签发时一致：批次导入/撤销或瑕疵标记变化即为 false；"
        "历史凭证不会因此被改写",
    )
    # 签发依据（历史冻结，永不改写；签发时三类阻断项全空故恒为空列表）
    issued_missing_ranges: list[list[int]] = Field(
        ..., description="签发依据：预期内缺帧区间（签发时为空）"
    )
    issued_out_of_range_frames: list[int] = Field(
        ..., description="签发依据：预期外帧（签发时为空）"
    )
    issued_defect_frames: list[int] = Field(
        ..., description="签发依据：全出现被标记瑕疵的帧（签发时为空）"
    )
    expected_ranges: list[list[int]] = Field(
        ..., description="签发依据：签发时冻结的预期覆盖闭区间"
    )
    frames: list[int] = Field(
        ..., description="签发依据：签发时冻结的实扫帧（去重排序）"
    )
    batches: list[DeliveryCheckBatch] = Field(
        ..., description="签发依据：签发时存活批次的身份"
    )
    defects: list[DeliveryCheckDefect] = Field(
        ..., description="签发依据：签发时全部瑕疵标记（位置与类型）"
    )
    state_fingerprint: str = Field(..., description="签发时冻结的状态指纹")
    created_at: str = Field(..., description="凭证签发时间（UTC ISO-8601）")
    # 按当前状态实时重算（只用于呈现凭证为何失效，不修改凭证）
    current_state_fingerprint: str = Field(
        ..., description="查询时按存活批次与瑕疵标记重算的状态指纹"
    )
    current_missing_ranges: list[list[int]] = Field(
        ..., description="查询时重算的预期内缺帧区间（胶卷无存活批次时为 []）"
    )
    current_out_of_range_frames: list[int] = Field(
        ..., description="查询时重算的预期外帧（胶卷无存活批次时为 []）"
    )
    current_defect_frames: list[int] = Field(
        ..., description="查询时重算的全出现被标记瑕疵帧（胶卷无存活批次时为 []）"
    )


class RollState(BaseModel):
    roll_no: str
    expected_ranges: list[list[int]]
    frames: list[int]
    missing_ranges: list[list[int]]
    out_of_range_frames: list[int]
    total_frames: int
    batch_count: int
    batches: list["BatchMeta"]


class BatchMeta(BaseModel):
    batch_id: str
    expected_start: int
    expected_end: int
    frames: list[int]
    added_frames: list[int]
    duplicates: list[int]
    created_at: str


class RollSummary(BaseModel):
    roll_no: str
    total_frames: int
    batch_count: int
    missing_ranges: list[list[int]]


class FrameBatchRef(BaseModel):
    batch_id: str = Field(
        ..., description="扫到该帧的有效批次；列表按批次原导入顺序排列"
    )
    occurrence_count: int | None = Field(
        ...,
        description="该帧在该批**原始帧列表**中的出现次数；"
        "旧批次没有原始列表时为 null（无法区分重复次数，不做推断）",
    )


class FrameProvenanceItem(BaseModel):
    frame_no: int
    first_scan_batch_id: str = Field(
        ...,
        description="最早有效批次（当前帧归属批次）；撤销批次后按剩余批次实时重算，首扫归属随之转移",
    )
    in_expected_coverage: bool = Field(
        ..., description="该帧是否位于胶卷当前预期覆盖（合并后的预期闭区间）内"
    )
    batches: list[FrameBatchRef] = Field(
        ..., description="扫描过该帧的全部有效批次，按批次原导入顺序排列"
    )


class FrameProvenanceOut(BaseModel):
    roll_no: str
    frames: list[FrameProvenanceItem] = Field(
        ..., description="本页帧的溯源信息，按帧号升序（只含实际存在的帧号）"
    )
    next_cursor: int | None = Field(
        ...,
        description="下一页游标（本页末帧号），传入 cursor 续读；无后续帧时为 null",
    )


# ---------- 胶卷母版选片 ----------

class MasterPickItem(BaseModel):
    frame_no: int = Field(..., description="帧号（位于请求区间与当前预期覆盖的交集内）")
    batch_id: str = Field(
        ...,
        description="该帧母版来源批次：在该帧全部合格候选中，按“段内批次切换次数最少、"
        "并列时逐帧比较批次原导入顺序与原始位置”选出的存活批次",
    )
    position: int | None = Field(
        ...,
        description="该帧在来源批次**原始帧列表**中的 0 起算位置（同批同帧多个合格出现"
        "时取最早位置）；旧批次只有去重帧、没有原始列表时为 null（位置为空的候选）",
    )


class MasterPicksOut(BaseModel):
    roll_no: str
    start: int = Field(..., description="请求帧号区间起始（含）")
    end: int = Field(..., description="请求帧号区间结束（含）")
    covered_ranges: list[list[int]] = Field(
        ...,
        description="请求闭区间与胶卷**当前预期覆盖**的交集（合并后的连续闭区间），"
        "即本次实际参与选片的全部范围",
    )
    picks: list[MasterPickItem] = Field(
        ...,
        description="交集内每个有合格候选的帧的选片结果，按帧号升序；"
        "未扫描帧与无合格候选帧不出现在此列表中"
    )
    unscanned_ranges: list[list[int]] = Field(
        ...,
        description="交集内**从未扫描**的连续闭区间（区间运算得出，不逐帧展开大跨度）",
    )
    no_candidate_frames: list[int] = Field(
        ...,
        description="交集内**已扫描却无合格候选**的帧号（升序）：该帧在存活批次中的"
        "全部扫描出现均已被标记瑕疵；这些帧会隔断连续可选帧段",
    )


class HealthOut(BaseModel):
    status: str
    db: str


class RescanClaimIn(BaseModel):
    claimant: str = Field(..., min_length=1, description="领取人标识")
    max_frames: PositiveInt = Field(..., description="本次最多领取的帧数（正整数）")
    ttl_seconds: PositiveInt = Field(..., description="认领有效秒数（正整数），到期后帧段自动让出")

    @field_validator("claimant")
    @classmethod
    def _strip_claimant(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("claimant 不能为空")
        return v


class RescanClaimOut(BaseModel):
    id: str
    roll_no: str
    claimant: str
    frame_start: int = Field(..., description="认领帧段起始（含）")
    frame_end: int = Field(..., description="认领帧段结束（含）")
    frame_count: int
    max_frames: int
    ttl_seconds: int
    created_at: str
    expires_at: str
    remaining_missing_ranges: list[list[int]] = Field(
        ..., description="该任务帧段中目前仍缺失（未扫到）的连续闭区间"
    )


class RescanReceiptIn(BaseModel):
    batch_id: str = Field(..., min_length=1, description="本次补扫批次号（必填）")
    frames: list[int] = Field(
        ..., min_length=1, description="本次实际扫到的非空帧号列表（原始列表，至少 1 个）"
    )

    @field_validator("batch_id")
    @classmethod
    def _strip_batch_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("batch_id 不能为空")
        return v


class RescanReceiptOut(BaseModel):
    id: str = Field(..., description="回执 ID")
    claim_id: str
    roll_no: str
    claimant: str
    frame_start: int = Field(..., description="认领帧段起始（含）")
    frame_end: int = Field(..., description="认领帧段结束（含）")
    batch_id: str = Field(..., description="本次补扫导入的批次号")
    frames: list[int] = Field(
        ..., description="提交的原始帧列表（含批内重复，保序）"
    )
    added_frames: list[int] = Field(..., description="本批新补齐的帧号（去重排序）")
    duplicates: list[int] = Field(
        ..., description="重复帧（批内重复 + 胶卷已有），只计一次"
    )
    repeated: bool = Field(
        ...,
        description="True=相同认领、批次号与原始帧列表的重试，回执为已存记录，未重复导入",
    )
    remaining_missing_ranges: list[list[int]] = Field(
        ..., description="结案后该认领帧段内仍缺失（未扫到）的连续闭区间"
    )
    created_at: str


class RollSnapshotOut(BaseModel):
    snapshot_id: str = Field(..., description="快照 ID，可长期用于差异查询")
    roll_no: str
    expected_ranges: list[list[int]] = Field(
        ..., description="固化时的预期覆盖闭区间（合并后）"
    )
    frames: list[int] = Field(..., description="固化时的实扫帧（去重排序）")
    missing_ranges: list[list[int]] = Field(
        ..., description="固化时的缺帧闭区间（预期范围内未扫到的连续段）"
    )
    created_at: str = Field(..., description="快照创建时间（UTC ISO-8601）")


class RangeDiff(BaseModel):
    added: list[list[int]] = Field(
        ..., description="当前相对快照**新增**的闭区间（当前有、快照无）"
    )
    removed: list[list[int]] = Field(
        ..., description="当前相对快照**消失**的闭区间（快照有、当前无）"
    )


class SnapshotDiffOut(BaseModel):
    snapshot_id: str
    roll_no: str
    roll_exists: bool = Field(
        ...,
        description="查询时胶卷是否存在（最后一批被撤销后为 false，此时当前状态按空处理）",
    )
    snapshot_created_at: str
    compared_at: str = Field(..., description="差异计算时间（UTC ISO-8601）")
    expected: RangeDiff = Field(..., description="预期覆盖的差异（区间集合运算）")
    frames: RangeDiff = Field(..., description="实扫帧的差异（帧集合先压成闭区间再比较）")
    missing: RangeDiff = Field(..., description="缺帧区间的差异（区间集合运算）")


RollState.model_rebuild()
