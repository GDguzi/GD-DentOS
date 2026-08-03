create table if not exists sync_batches (
    batch_id text primary key,
    source text not null,
    status text not null,
    started_at text not null,
    finished_at text,
    summary_json text not null default '{}'
);

create table if not exists patients (
    patient_identity text primary key,
    source_customer_id text,
    display_name text,
    phone text,
    phone_vestee text not null default '',        -- 手机机主关系(本人/爸爸/妈妈… PhoneVestee字典)
    sex text,
    birthday text,
    address text,
    allergy_history text not null default '',     -- 过敏史(本地业务列, P0-2; 不入 source_json 镜像)
    medication_history text not null default '',  -- 用药史(本地业务列, P0-2)
    id_card text not null default '',             -- 身份证号(本地业务列, P0-3)
    wechat text not null default '',              -- 微信(P0-3)
    email text not null default '',               -- 邮箱(P0-3)
    occupation text not null default '',          -- 职业(P0-3)
    work_unit text not null default '',           -- 工作单位(P0-3)
    patient_type text not null default '',        -- 患者类型(P0-3)
    responsible_doctor text not null default '',  -- 责任医生(P1-6)
    consultant text not null default '',          -- 咨询师(P1-6)
    referral_source text not null default '',     -- 患者来源槽1(存树路径,如 家住附近/碧桂园)
    referral_source2 text not null default '',    -- 患者来源槽2
    referral_source3 text not null default '',    -- 患者来源槽3
    referral_source4 text not null default '',    -- 患者来源槽4(对齐官方4槽)
    introducer_type text not null default '',     -- 介绍人类型: 患者介绍/员工介绍
    introducer_name text not null default '',     -- 介绍人姓名(患者或员工)
    chart_no text not null default '',            -- 病历号(同步=SaaS patientid,本地建=YYMMDD+流水)
    name_pinyin text not null default '',         -- 拼音搜索
    avatar_path text not null default '',         -- 头像
    remark text not null default '',              -- 备注(本地建档/前台填写)
    merged_into text not null default '',         -- 被合并到的主患者 patient_identity
    is_deleted integer not null default 0,        -- 合并/删除标记,列表与计数排除
    referral_doctor text not null default '',     -- 介绍/转诊医生(从 source_json 回填)
    patient_group text not null default '',        -- 患者分组(从 source_json 回填)
    past_history text not null default '',          -- 既往病史(从 source_json 回填)
    updated_at text,
    current_hash text not null,
    source_json text not null default '{}',
    created_at text not null default current_timestamp,
    local_updated_at text not null default current_timestamp,
    last_batch_id text references sync_batches(batch_id)
);

-- 患者来源三级树(本地可配置,对齐官方"患者来源设置"页)：一级/二级/三级,父子结构
create table if not exists referral_sources (
    source_id text primary key,
    parent_id text not null default '',           -- '' = 一级；否则指向父节点 source_id
    level integer not null default 1,             -- 1/2/3
    name text not null,
    display_order integer not null default 0,
    hidden integer not null default 0,            -- 隐藏(不在选择器出现,但不删)
    created_at text not null default current_timestamp
);
create index if not exists idx_referral_sources_parent on referral_sources(parent_id, display_order);

create unique index if not exists idx_patients_source_customer_id
on patients(source_customer_id)
where source_customer_id is not null and source_customer_id != '';

-- 收费框架A：病历号唯一(空值不约束)。挡住并发建档/回填撞号
create unique index if not exists ux_patients_chart_no
on patients(chart_no)
where chart_no <> '';

create table if not exists patient_versions (
    version_id integer primary key autoincrement,
    patient_identity text not null references patients(patient_identity),
    version_hash text not null,
    source_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

create table if not exists medical_records (
    record_id text primary key,
    patient_identity text not null references patients(patient_identity),
    study_identity text,
    record_type text,
    visit_time text,
    doctor_name text,
    tooth_json text not null default '{}',
    content_json text not null default '{}',
    draft_status text not null default '',
    origin text not null default '',
    ai_meta_json text not null default '{}',
    created_at text,
    updated_at text,
    current_hash text not null,
    last_batch_id text references sync_batches(batch_id)
);

create table if not exists medical_record_versions (
    version_id integer primary key autoincrement,
    record_id text not null references medical_records(record_id),
    version_hash text not null,
    snapshot_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

create table if not exists appointments (
    appointment_id text primary key,
    patient_identity text not null references patients(patient_identity),
    study_identity text,
    start_time text,
    end_time text,
    doctor_name text,
    item_name text,
    status text not null default '',
    cancel_reason text not null default '',
    room text not null default '',
    arrived_at text not null default '',
    finished_at text not null default '',
    visit_type text not null default '',                  -- 分诊就诊类型: 初诊/复诊/新诊
    register_type text not null default '预约',           -- 登记方式: 预约 / 到店(walk-in)
    last_batch_id text default '',                        -- 同步冲突窗口(本地编辑判定)
    suspect_cancelled integer not null default 0,         -- 7天窗口对账:SaaS已无→疑似取消(待核对)
    suspect_reason text not null default '',              -- 疑似取消的批次/原因
    schedule_remark text not null default '',             -- 预约备注(从 source_json 回填)
    source_json text not null default '{}',
    created_at text not null default '',                  -- 本地创建时间(本地新增才填;同步进来留空)。今日工作台「预约」按钮高亮据此判"今天约的"
    updated_at text
);

create table if not exists appointment_versions (
    version_id integer primary key autoincrement,
    appointment_id text not null references appointments(appointment_id),
    version_hash text not null,
    snapshot_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

create table if not exists return_visits (
    return_visit_id text primary key,
    patient_identity text not null references patients(patient_identity),
    due_time text,
    item_name text,
    status text not null default '',
    note text not null default '',
    visitor text not null default '',
    return_doctor text not null default '',
    return_type text not null default '',
    channel text not null default '',
    return_result text not null default '',
    actual_date text not null default '',
    is_deleted integer not null default 0,
    origin text not null default '',
    intent_level text not null default '',
    satisfaction text not null default '',
    last_batch_id text default '',                        -- 同步冲突窗口(本地编辑判定)
    source_json text not null default '{}',
    created_at text not null default '',                  -- 本地创建时间(本地新增才填;同步进来留空)。今日工作台「回访」按钮高亮据此判"今天新增的回访"
    revision integer not null default 1,
    updated_at text
);

create table if not exists return_visit_versions (
    version_id integer primary key autoincrement,
    return_visit_id text not null references return_visits(return_visit_id),
    version_hash text not null,
    snapshot_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

-- 患者专属标签(精品路线·大客户经营)：VIP/怕疼/偏好/忌讳等自由打标，按标签可筛大客户
create table if not exists patient_tags (
    tag_id text primary key,
    patient_identity text not null references patients(patient_identity),
    name text not null,
    color text not null default 'gray',
    created_at text not null default current_timestamp,
    operator text not null default ''
);
create index if not exists idx_patient_tags_pid on patient_tags(patient_identity);

create table if not exists bills (
    bill_id text primary key,
    patient_identity text not null references patients(patient_identity),
    bill_no text,
    bill_time text,
    total_fee real not null default 0,
    paid_fee real not null default 0,
    state text not null default '',
    bill_doctor text not null default '',           -- 开单医生(从 source_json 回填)
    source_json text not null default '{}',
    updated_at text
);

create table if not exists payments (
    payment_id text primary key,
    patient_identity text not null references patients(patient_identity),
    bill_id text,
    pay_time text,
    amount real not null default 0,
    state text not null default '',
    pay_type text not null default '',            -- 支付方式: 现金/微信/支付宝/银行卡/社保卡等，候选名单在设置里配 (P1-12)
    payment_record_type text not null default '', -- 收退类型: charge 收费 / refund 退费 (P1-12)
    operator text not null default '',            -- 收款员(收费框架B)
    invoice_no text not null default '',          -- 发票号(收费框架B)
    invoice_remark text not null default '',      -- 发票备注(收费框架B)
    remark text not null default '',              -- 收款备注(收费框架B)
    source_json text not null default '{}',
    updated_at text
);

-- 收退款幂等登记(#800)：前端每次收款/退费意图生成一次性 request_id,同号同载荷重放返回首次
-- 结果(不再落第二笔),同号异载荷 409。只记成功请求;校验失败不记,改参重试不受阻。
create table if not exists payment_requests (
    request_id text primary key,                  -- 前端 crypto.randomUUID()
    bill_id text not null,
    kind text not null,                           -- pay / refund
    payload_hash text not null,                   -- sha256(去 request_id 后的规范化 JSON)
    response_json text not null,                  -- 首次成功响应,重放原样返回
    created_at text not null
);

-- 账单/支付导入覆盖前的版本快照（改钱保护，#309）：本地收款/退费过的账单不被 SaaS 回填静默覆盖。
create table if not exists bill_versions (
    version_id integer primary key autoincrement,
    bill_id text not null references bills(bill_id),
    version_hash text not null,
    snapshot_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

create table if not exists payment_versions (
    version_id integer primary key autoincrement,
    payment_id text not null references payments(payment_id),
    version_hash text not null,
    snapshot_json text not null,
    batch_id text references sync_batches(batch_id),
    changed_at text not null default current_timestamp
);

-- 双跑对账：每天 本地 vs SaaS 九维(预约/患者/账单/欠费/收款/退费/治疗项目/病历/影像)差异,差异≠0 报警。
-- diff_json：每维度差异记录 id(missing_local/surplus_local) + 备注(如某维度不可对账)。
create table if not exists reconcile_log (
    recon_date text primary key,
    local_appts integer not null default 0,
    saas_appts integer not null default 0,
    local_income real not null default 0,
    saas_income real not null default 0,
    local_patients integer not null default 0,
    saas_patients integer not null default 0,
    local_bills integer not null default 0,
    saas_bills integer not null default 0,
    local_arrears real not null default 0,
    saas_arrears real not null default 0,
    local_refunds real not null default 0,
    saas_refunds real not null default 0,
    local_items integer not null default 0,
    saas_items integer not null default 0,
    local_records integer not null default 0,
    saas_records integer not null default 0,
    local_images integer not null default 0,
    saas_images integer not null default 0,
    local_images_done integer not null default 0,
    diff_json text not null default '{}',
    run_at text
);

create table if not exists image_download_tasks (
    task_id text primary key,
    patient_identity text not null,
    source_url text not null default '',
    upload_date text,
    image_type text not null default '',
    archive_path text not null default '',
    status text not null default 'pending',
    error_text text not null default '',
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);

create index if not exists idx_image_download_tasks_status
on image_download_tasks(status, created_at);

create index if not exists idx_image_download_tasks_patient
on image_download_tasks(patient_identity, upload_date);

create table if not exists local_notes (
    note_id text primary key,
    patient_identity text not null references patients(patient_identity),
    note_text text not null,
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);

create table if not exists audit_logs (
    audit_id integer primary key autoincrement,
    entity_type text not null,
    entity_id text not null,
    action text not null,
    old_json text,
    new_json text,
    operator text,
    created_at text not null default current_timestamp
);

create index if not exists idx_patients_search
on patients(display_name, phone, source_customer_id, patient_identity);

create index if not exists idx_medical_records_patient
on medical_records(patient_identity, visit_time);

create table if not exists import_conflicts (
    conflict_id integer primary key autoincrement,
    entity_type text not null,
    entity_id text not null,
    field_name text not null,
    local_value text,
    incoming_value text,
    batch_id text references sync_batches(batch_id),
    status text not null default 'open',
    conflict_key text not null default '',
    reason_code text not null default 'value_conflict',
    created_at text not null default current_timestamp
);

create unique index if not exists idx_import_conflicts_conflict_key
on import_conflicts(conflict_key)
where conflict_key <> '';

create table if not exists treatment_items (
    treatment_item_id text primary key,
    patient_identity text not null references patients(patient_identity),
    bill_id text,
    item_name text,
    item_code text,
    handleset_identity text,
    doctor_name text,
    unit_price real not null default 0,
    quantity real not null default 0,
    total_fee real not null default 0,
    fee_type text not null default '',
    is_deleted integer not null default 0,
    is_refund integer not null default 0,
    refunded_fee real not null default 0,                 -- #573 累计已退金额:退满才标 is_refund,部分退不再永久锁死
    order_id text default '',                             -- 归属本地处置单(划价前无 bill_id)
    tooth_codes text default '',                           -- 处置牙位 FDI(从 t_billhandle 回填,哪颗牙做了哪个处置)
    source_json text not null default '{}',
    updated_at text,
    last_batch_id text references sync_batches(batch_id)
);

create index if not exists idx_treatment_items_patient
on treatment_items(patient_identity, bill_id);

create index if not exists idx_treatment_items_bill
on treatment_items(bill_id);

create table if not exists patient_images (
    image_id text primary key,
    patient_identity text not null references patients(patient_identity),
    image_date text,
    seq integer,
    category text not null default '',
    file_name text not null,
    rel_path text not null,
    size_bytes integer not null default 0,
    content_hash text not null default '',
    tooth_codes text not null default '',         -- 牙位(逗号FDI):本地拍摄按机位模板带出/主诉牙手选
    source_folder text not null default '',
    last_batch_id text references sync_batches(batch_id),
    created_at text not null default current_timestamp
);

create unique index if not exists idx_patient_images_rel_path
on patient_images(rel_path);

create index if not exists idx_patient_images_patient
on patient_images(patient_identity, image_date);

create table if not exists ai_unclaimed_drafts (
    unclaimed_id text primary key,
    code_name text,
    visit_time text,
    room text,
    doctor_name text,
    content_json text not null default '{}',
    tooth_json text not null default '{}',
    ai_meta_json text not null default '{}',
    source_file text,
    status text not null default 'pending',
    created_at text not null default current_timestamp
);

create unique index if not exists idx_ai_unclaimed_drafts_source_file
on ai_unclaimed_drafts(source_file);

create index if not exists idx_ai_unclaimed_drafts_status
on ai_unclaimed_drafts(status, created_at);

create table if not exists medical_templates (
    template_id text primary key,
    template_name text not null,
    category text default '',
    content_json text not null default '{}',
    source_ids text default '',
    active integer not null default 1,
    created_at text,
    updated_at text
);

create index if not exists idx_medical_templates_category
on medical_templates(category, template_name);

-- M2 主数据：处置项目（纯价目字典，无隐私字段）
create table if not exists handle_items (
    handle_id text primary key,
    code text default '',
    name text not null,
    unit text default '',
    price real,
    handle_type text default '',
    fee_type text default '',
    name_py text default '',
    stopped integer not null default 0,
    source_json text not null default '{}',
    last_batch_id text default '',
    created_at text,
    updated_at text
);

create index if not exists idx_handle_items_type
on handle_items(handle_type, name);
create index if not exists idx_handle_items_code
on handle_items(code);

-- M2 主数据：全局字典（原系统 t_dictionary 同步，枚举值，无隐私字段）
create table if not exists dictionaries (
    dict_id text primary key,
    dict_type text default '',
    name text not null,
    value text default '',
    describe text default '',
    display_order integer not null default 0,
    stopped integer not null default 0,
    source_json text not null default '{}',
    last_batch_id text default '',
    created_at text,
    updated_at text
);

create index if not exists idx_dictionaries_type
on dictionaries(dict_type, display_order);

-- === 治疗计划 (P1-⑤) ===
create table if not exists treatment_plans (
    plan_id text primary key,
    patient_identity text not null,
    plan_name text default '',
    plan_date text default '',
    doctor_name text default '',
    status text not null default 'draft',   -- draft/confirmed/done/withdrawn
    total_price real,
    bill_id text default '',                 -- 已转划价生成的收费单(防重复划价)
    source text not null default 'local',
    note text default '',
    category text not null default '',          -- P2-28 计划类别
    label text not null default '',             -- P2-28 标签
    diagnosis text not null default '',         -- P2-28 诊断
    treatment_goals text not null default '',   -- P2-28 治疗目标
    precautions text not null default '',       -- P2-28 注意事项
    is_deleted integer not null default 0,      -- 软删(删除按钮)
    source_json text not null default '{}',      -- SaaS 导入:原始行,审计/回溯
    last_batch_id text default '',               -- SaaS 导入:同步批次
    created_at text,
    updated_at text
);

create index if not exists idx_treatment_plans_patient
on treatment_plans(patient_identity, plan_date);

create table if not exists treatment_plan_items (
    item_id text primary key,
    plan_id text not null,
    group_id text default '',          -- 所属方案分组（空=未分组，兼容老数据/老接口）
    handle_id text default '',
    item_name text not null,
    tooth text default '',
    quantity integer not null default 1,
    unit_price real,
    total_price real,
    sort_order integer not null default 0,
    note text default ''
);

create index if not exists idx_treatment_plan_items_plan
on treatment_plan_items(plan_id, sort_order);

-- === 治疗计划方案层（分组：都做/二选一） ===
create table if not exists treatment_plan_groups (
    group_id text primary key,
    plan_id text not null,
    group_name text default '',
    group_type text not null default 'all',   -- all=都做(分类清单) / alt=二选一(备选方案)
    selected_item_id text default '',          -- alt组病人选中的处置item_id；all组留空
    sort_order integer not null default 0,
    created_at text,
    updated_at text
);

create index if not exists idx_treatment_plan_groups_plan
on treatment_plan_groups(plan_id, sort_order);

-- === 口腔检查/全口牙位图 (P1-④) ===
create table if not exists oral_exams (
    exam_id text primary key,
    patient_identity text not null,
    exam_date text default '',
    exam_doctor text default '',
    health_level text default '',          -- 健康/良好/一般/不佳
    next_followup text default '',
    source text not null default 'local',
    note text default '',
    created_at text,
    updated_at text
);

create index if not exists idx_oral_exams_patient
on oral_exams(patient_identity, exam_date);

create table if not exists oral_exam_findings (
    finding_id text primary key,
    exam_id text not null,
    tooth text default '',                 -- 牙位编码(如 11/36 或 Palmer)
    symptom text not null,
    severity text default '',              -- 较轻/中等/严重
    suggest_time text default '',
    note text default ''
);

create index if not exists idx_oral_exam_findings_exam
on oral_exam_findings(exam_id);

-- === 咨询沟通/面诊 (P3-⑪) ===
create table if not exists consults (
    consult_id text primary key,
    patient_identity text not null,
    consult_date text default '',
    consult_doctor text default '',
    consult_type text default '',          -- 沟通类型
    chief_complaint text default '',       -- 主诉/需求
    plan text default '',                  -- 医生方案
    communication text default '',         -- 沟通记录
    potential text default '',             -- 潜在需求
    advice text default '',                -- 服务建议
    source text not null default 'local',
    created_at text,
    updated_at text
);

create index if not exists idx_consults_patient
on consults(patient_identity, consult_date);

-- === 手术记录 (P4-⑭) ===
create table if not exists surgeries (
    surgery_id text primary key,
    patient_identity text not null,
    surgery_date text default '',
    surgeon text default '',
    surgery_name text not null,
    tooth text default '',
    anesthesia text default '',
    process text default '',               -- 手术过程
    postop_advice text default '',         -- 术后医嘱
    source text not null default 'local',
    created_at text,
    updated_at text
);

create index if not exists idx_surgeries_patient
on surgeries(patient_identity, surgery_date);

-- === 牙位结构化病历条目 (病历骨架) ===
create table if not exists medical_record_items (
    item_id text primary key,
    record_id text not null references medical_records(record_id),
    patient_identity text not null references patients(patient_identity),
    tooth text not null default '',        -- 单颗牙位码(FDI, 如 11/36)；空=全口/记录级
    item_type text not null,               -- exam检查/diagnosis诊断/treatment治疗/plan计划
    content text not null default '',
    sort_order integer not null default 0,
    created_at text,
    updated_at text
);

create index if not exists idx_record_items_record
on medical_record_items(record_id, sort_order);

create index if not exists idx_record_items_patient_tooth
on medical_record_items(patient_identity, tooth);

-- === 性能索引补齐 (患者详情页5表按patient_identity过滤、审计页按实体/时间过滤) ===
create index if not exists idx_appointments_patient
on appointments(patient_identity, start_time);

create index if not exists idx_return_visits_patient
on return_visits(patient_identity, due_time);

create index if not exists idx_bills_patient
on bills(patient_identity, bill_time);

create index if not exists idx_payments_patient
on payments(patient_identity, pay_time);

-- #800/#813①:按账单查退款流水(可退额度派生/退款/回填迁移)避免全表扫,大额历史付款库下必需
create index if not exists idx_payments_bill_type
on payments(bill_id, payment_record_type);

create index if not exists idx_local_notes_patient
on local_notes(patient_identity);

create index if not exists idx_audit_logs_entity
on audit_logs(entity_type, entity_id);

create index if not exists idx_audit_logs_created
on audit_logs(created_at);

-- === 库房 ===
create table if not exists stock_category (
    id text primary key,
    name text not null,
    parent_id text default '',
    sort integer not null default 0,
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);

create index if not exists idx_stock_category_parent
on stock_category(parent_id, sort);

create table if not exists stock_item (
    id text primary key,
    code text not null unique,
    name text not null,
    category_id text default '',
    spec text default '',
    unit text default '',
    brand text default '',
    price real,
    is_high_value integer not null default 0,
    min_qty real,
    max_qty real,
    shelf_life_days integer,
    status text not null default 'active',
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);

create index if not exists idx_stock_item_name
on stock_item(name);

create index if not exists idx_stock_item_category
on stock_item(category_id, name);

create table if not exists supplier (
    id text primary key,
    name text not null,
    contact text default '',
    phone text default '',
    note text default '',
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);

create index if not exists idx_supplier_name
on supplier(name);

create table if not exists stock_in (
    id text primary key,
    in_no text not null unique,
    type text not null,                   -- purchase/return
    supplier_id text default '',
    operator text default '',
    handler text default '',
    status text not null default 'draft', -- draft/confirmed
    is_deleted integer not null default 0,
    confirmed_at text,
    created_at text,
    updated_at text
);

create index if not exists idx_stock_in_status
on stock_in(status, created_at);

create table if not exists stock_in_item (
    id text primary key,
    stock_in_id text not null,
    stock_item_id text not null,
    qty real not null,
    unit_price real,
    batch_no text default '',
    expire_date text default '',
    track_code text default ''
);

create index if not exists idx_stock_in_item_doc
on stock_in_item(stock_in_id);

create index if not exists idx_stock_in_item_item
on stock_in_item(stock_item_id);

create table if not exists stock_balance (
    id text primary key,
    stock_item_id text not null,
    batch_no text not null default '',
    expire_date text not null default '',
    track_code text not null default '',
    qty real not null default 0,
    updated_at text
);

create unique index if not exists idx_stock_balance_key
on stock_balance(stock_item_id, batch_no, expire_date, track_code);

create index if not exists idx_stock_balance_item
on stock_balance(stock_item_id);

create table if not exists stock_ledger (
    id text primary key,
    stock_item_id text not null,
    change_qty real not null,
    balance_qty real not null,
    source_type text not null,
    source_id text not null,
    source_item_id text default '',
    batch_no text default '',
    expire_date text default '',
    track_code text default '',
    note text default '',
    created_at text
);

create index if not exists idx_stock_ledger_item
on stock_ledger(stock_item_id, created_at);

create index if not exists idx_stock_ledger_source
on stock_ledger(source_type, source_id);

create table if not exists stock_out (
    id text primary key,
    out_no text not null unique,
    type text not null,                    -- use/return_supplier
    operator text default '',
    handler text default '',
    receiver text default '',
    supplier_id text default '',
    status text not null default 'draft',  -- draft/confirmed
    is_deleted integer not null default 0,
    confirmed_at text,
    created_at text,
    updated_at text
);

create index if not exists idx_stock_out_status
on stock_out(status, created_at);

create table if not exists stock_out_item (
    id text primary key,
    stock_out_id text not null,
    stock_item_id text not null,
    qty real not null,
    track_code text default ''
);

create index if not exists idx_stock_out_item_doc
on stock_out_item(stock_out_id);

create index if not exists idx_stock_out_item_item
on stock_out_item(stock_item_id);

create table if not exists stock_take (
    id text primary key,
    take_no text not null unique,
    operator text default '',
    status text not null default 'draft',  -- draft/confirmed
    is_deleted integer not null default 0,
    confirmed_at text,
    created_at text,
    updated_at text
);

create index if not exists idx_stock_take_status
on stock_take(status, created_at);

create table if not exists stock_take_item (
    id text primary key,
    stock_take_id text not null,
    stock_item_id text not null,
    book_qty real not null,
    actual_qty real not null,
    diff real not null
);

create index if not exists idx_stock_take_item_doc
on stock_take_item(stock_take_id);

create index if not exists idx_stock_take_item_item
on stock_take_item(stock_item_id);

-- === 处置单（两步：新增处置 待划价 → 划价 待收费 → 收费 已收费 / 撤销） ===
create table if not exists treatment_orders (
    order_id text primary key,
    order_no text not null default '',         -- 本地处置单号 CZ+YYMMDD+当日流水(避开SaaS的B号)
    visit_type text not null default '',       -- 就诊类型(初诊/复诊/新诊),开单时从当日预约带入
    reopened integer not null default 0,       -- 离开关闭后被「撤回」解锁过(覆盖离开锁定)
    patient_identity text not null,
    order_date text default '',
    doctor_name text default '',
    nurse_name text default '',         -- 配台护士
    consultant_name text default '',    -- 配台咨询师
    assistant_name text default '',     -- 配台助理
    diagnosis text default '',
    status text not null default 'recorded',  -- recorded待划价/priced待收费/paid已收费/voided已撤销
    bill_id text default '',                   -- 划价后生成的收费单
    discount real not null default 0,
    created_at text,
    updated_at text
);
create index if not exists idx_treatment_orders_patient
on treatment_orders(patient_identity, created_at);

-- === 配台人员（医生/护士/咨询师/助理，本地录入，供处置配台下拉+绩效统计） ===
create table if not exists staff_members (
    staff_id text primary key,
    name text not null,
    role text not null default '',     -- 医生/护士/咨询师/助理
    note text default '',
    active integer not null default 1,  -- 软删=0
    job_no text not null default '',       -- P2-31 工号
    phone text not null default '',        -- P2-31 手机
    sex text not null default '',          -- P2-31 性别
    id_card text not null default '',      -- P2-31 身份证号
    title text not null default '',        -- P2-31 职称
    license_no text not null default '',   -- P2-31 执业证号
    department text not null default '',   -- P2-31 科室
    roles text not null default '',        -- 多岗位(逗号包裹 ",医生,助理,")，role 存主岗位；选人下拉按 roles 匹配
    created_at text,
    updated_at text
);
create index if not exists idx_staff_members_role on staff_members(role, active);

-- === 知情同意书 (#15)：模板 + 签署件。模板=诊所现行同意书正文；签署件=填充+手写签名+内容哈希(防篡改)+可信时间戳(TSA,二期) ===
create table if not exists consent_templates (
    template_id text primary key,
    name text not null,                 -- 拔牙知情同意书
    category text not null default '',  -- 拔牙/根管/种植/正畸/修复... (供按处置类别匹配)
    body text not null default '',      -- 正文纯文本(段落\n分隔)
    source text not null default '',    -- 来源文件名(追溯)
    active integer not null default 1,
    created_at text,
    updated_at text
);
create index if not exists idx_consent_templates_cat on consent_templates(category, active);

create table if not exists consent_documents (
    document_id text primary key,
    patient_identity text not null,
    template_id text not null default '',
    template_name text not null default '',
    bill_id text not null default '',       -- 关联收费单(已划价时;同意书与费用一起出)
    order_id text not null default '',       -- 关联处置单(未划价先签时绑这,稳定可追溯)
    content_text text not null default '',  -- 填充后正文快照
    content_json text not null default '{}',-- 患者信息/费用清单/付款方式/字段值
    patient_sign text not null default '',  -- 患者手写签名(base64 PNG)
    doctor_sign text not null default '',   -- 医生手写签名
    content_hash text not null default '',  -- 内容哈希(防篡改)
    tsa_token text not null default '',     -- 可信时间戳(TSA返回,二期填)
    status text not null default 'signed',  -- signed已签/voided作废重签
    signed_at text,
    created_at text,
    updated_at text
);
create index if not exists idx_consent_documents_patient on consent_documents(patient_identity, created_at);
create index if not exists idx_consent_documents_bill on consent_documents(bill_id);
create index if not exists idx_consent_documents_order on consent_documents(order_id);

-- === 消毒/灭菌追溯 · 器械库(首期) ===
create table if not exists instrument_category (
    id text primary key,
    name text not null,
    parent_id text default '',
    sort integer not null default 0,
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);
create index if not exists idx_instrument_category_parent on instrument_category(parent_id, sort);

create table if not exists instrument (
    id text primary key,
    code text not null,                    -- 物品编号(未删范围内唯一)
    name text not null,
    category_id text default '',
    status text not null default 'active', -- active启用/disabled停用
    spec text default '',
    note text default '',
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);
-- 编号仅在未软删的器械间唯一(软删后编号可复用，避开全局unique占坑)
create unique index if not exists idx_instrument_code_active on instrument(code) where is_deleted = 0;
create index if not exists idx_instrument_name on instrument(name);
create index if not exists idx_instrument_category on instrument(category_id, name);

-- === 消毒/灭菌追溯 · 器械送消单(第1环) ===
create table if not exists sterilize_dispatch (
    id text primary key,
    dispatch_no text not null,             -- 送消单号(未删范围唯一,全链路串联键)
    department text default '',
    dispatcher text default '',            -- 送消人
    status text not null default 'draft',  -- draft草稿/submitted已送审/audited已审核/cancelled已取消
    auditor text default '',
    audited_at text default '',
    note text default '',
    is_deleted integer not null default 0,
    created_at text,
    updated_at text
);
create unique index if not exists idx_dispatch_no_active on sterilize_dispatch(dispatch_no) where is_deleted = 0;
create index if not exists idx_dispatch_status on sterilize_dispatch(status, created_at);

create table if not exists sterilize_dispatch_item (
    id text primary key,
    dispatch_id text not null,
    instrument_id text not null,
    qty real not null default 1,
    created_at text
);
create index if not exists idx_dispatch_item_dispatch on sterilize_dispatch_item(dispatch_id);

-- === 通用应用设置(KV) ===
create table if not exists app_settings (
    key text primary key,
    value text not null default '{}',
    updated_at text
);

-- === 账号系统(用户/会话) ===
create table if not exists users (
    id text primary key,
    username text not null unique,
    display_name text not null default '',
    password_hash text not null,
    role text not null default 'reception',   -- admin/reception/doctor/assistant（取值放宽为 roles.role_key）
    is_active integer not null default 1,
    staff_id text not null default '',         -- 关联 staff_members.staff_id（配台人员开通登录账号；空=独立账号）
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);
create table if not exists sessions (
    token text primary key,
    user_id text not null,
    created_at text not null default current_timestamp,
    -- v3开闸踩坑(2026-07-17):此列不再 NOT NULL——access_v3 的会话表按 personnel_access_schema
    -- 重建(用 idle_expires_at,无此列),启动自动补列曾把 NOT NULL 焊回去导致 v3 登录必炸。
    -- 旧栈代码始终显式写入此列,放宽为可空对旧栈无影响。
    expires_at text
);
create index if not exists idx_sessions_user on sessions(user_id);

-- ========================================================================
-- === 角色权限矩阵 (RBAC)：roles 角色定义 + role_permissions 角色×权限点 ===
-- 可配置：管理员在权限矩阵页勾选每角色能进哪些模块/做哪些操作；admin 代码层永远全通过(不入表)。
-- 权限点清单是代码常量(auth.PERMISSION_DEFS)，本表只存"角色勾了哪些 key"的稀疏关系。
-- ========================================================================
create table if not exists roles (
    role_key text primary key,             -- admin/doctor/nurse/reception/consultant + 自定义
    name text not null,                    -- 显示名：管理员/医生/护士/前台/咨询师
    is_system integer not null default 0,  -- 1=预置模板，不可删(权限可改)
    sort integer not null default 0,
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);
create table if not exists role_permissions (
    role_key text not null,
    perm_key text not null,                -- auth.PERMISSIONS 之一，如 patient.edit
    primary key (role_key, perm_key)
);
create index if not exists idx_role_perms_role on role_permissions(role_key);

-- === 按用户个性化设置(列自定义等) ===
create table if not exists user_settings (
    user_id text not null,
    key text not null,
    value text not null default '{}',
    updated_at text,
    primary key (user_id, key)
);

-- === 技工单（送技工所做义齿/正畸等：技工所/项目/比色 + 送出/返回日期 + 牙位 + 配图） ===
-- 技工所/项目/比色三类候选项存 app_settings(键 lab_factories/lab_projects/lab_colors)，不另建表
create table if not exists lab_orders (
    order_id text primary key,
    patient_identity text not null,
    tooth_codes text not null default '',   -- 牙位(逗号分隔FDI编码)
    project_type text not null default '',  -- 项目(如全瓷冠/活动义齿/正畸保持器)
    color text not null default '',         -- 比色(如A2/B1)
    lab_name text not null default '',      -- 技工所名称
    sent_date text not null default '',     -- 送出日期
    return_date text not null default '',   -- 返回日期(回件后填)
    status text not null default 'sent',    -- sent已送出 / returned已返回
    notes text not null default '',
    created_at text,
    updated_at text
);
create index if not exists idx_lab_orders_patient on lab_orders(patient_identity, created_at);

create table if not exists lab_order_images (
    img_id text primary key,
    lab_order_id text not null,
    rel_path text not null,                 -- images_dir 下的相对路径
    file_name text not null default '',
    created_at text
);
create index if not exists idx_lab_order_images_order on lab_order_images(lab_order_id);

-- === 会员储值（纯储值最小闭环：充值/消费扣减/退储值；本地业务，不回写 SaaS）===
create table if not exists member_accounts (
    patient_identity text primary key references patients(patient_identity),
    balance real not null default 0,        -- 当前储值余额(元)
    created_at text,
    updated_at text
);
create table if not exists member_transactions (
    txn_id text primary key,                -- local-mtxn- 前缀
    patient_identity text not null references patients(patient_identity),
    txn_type text not null,                 -- topup充值 / consume消费 / refund退储值
    amount real not null,                   -- 正数，方向由 txn_type 决定
    balance_after real not null,            -- 该笔后的余额(审计链)
    note text not null default '',
    bill_id text not null default '',       -- 消费关联账单(可空)
    operator text not null default '',
    created_at text
);
create index if not exists idx_member_txn_patient on member_transactions(patient_identity, created_at);

-- === 影像组图标记(组图查看:医生点"做组图"的患者+日期;自动不判组,人点了才算) ===
create table if not exists patient_image_sets (
    patient_identity text not null,
    image_date text not null,               -- YYYY-MM-DD
    created_at text,
    primary key (patient_identity, image_date)
);

-- === 通话录音(电话沟通:呼入/呼出录音入病历;网络电话/本地总机录音,详见 routes/calls.py) ===
-- 独立表,不污染 consults/return_visits;录音文件复用 patient_assets 落盘范式(表里只存相对路径+元数据)。
create table if not exists calls (
    call_id text primary key,
    direction text not null default '',         -- inbound 呼入 / outbound 呼出
    source text not null default '',            -- manual_import 手动导入 / return_visit_mobile 回访手机录音 / pbx 总机(Phase2)
    caller_raw text not null default '',        -- 主叫原始号
    callee_raw text not null default '',        -- 被叫原始号
    peer_norm text not null default '',         -- 对方号码归一(呼入取主叫/呼出取被叫,去空格/+86/前导0),匹配/展示用
    region text not null default '',            -- 地区(区号推导,留待)
    patient_identity text not null default '',  -- 关联患者;空=未认领
    match_status text not null default '',      -- manual 手挂 / matched 号码唯一命中 / unmatched 查无 / ambiguous 多人共号
    extension text not null default '',         -- 分机(哪个诊室拨/接)
    operator text not null default '',          -- 经手员工
    started_at text not null default '',        -- 通话开始(北京时间)
    duration_sec integer not null default 0,    -- 时长(秒)
    deal_status text not null default '',       -- 成交状态(未成交/已成交/意向/无效...)
    rel_path text not null default '',          -- 录音文件相对 images_dir 路径
    file_name text not null default '',
    size_bytes integer not null default 0,
    content_hash text not null default '',
    linked_consult_id text not null default '',        -- 弱关联到某条面诊(可空,不加外键约束以免污染既有表流程)
    linked_return_visit_id text not null default '',   -- 弱关联到某条回访
    linked_communication_id text not null default '',  -- 弱关联到某条沟通记录(电话咨询录音)
    pbx_uniqueid text not null default '',      -- 总机侧唯一ID(Phase2 幂等去重)
    note text not null default '',
    revision integer not null default 1,
    created_at text not null default ''
);
create index if not exists idx_calls_patient on calls(patient_identity, started_at);
create index if not exists idx_calls_peer on calls(peer_norm);
create unique index if not exists idx_calls_uniqueid on calls(pbx_uniqueid) where pbx_uniqueid <> '';
create index if not exists idx_calls_linked_rv
on calls(linked_return_visit_id);
create index if not exists idx_calls_linked_communication
on calls(linked_communication_id);

create table if not exists call_transcripts (
    call_id text primary key references calls(call_id) on delete cascade,
    status text not null,
    text text not null default '',
    segments_json text not null default '[]',
    model text not null default '',
    error text not null default '',
    duration_sec real,
    summary_status text not null default 'disabled',
    summary_json text not null default '',
    summary_model text not null default '',
    summary_error text not null default '',
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);

create table if not exists attachment_file_ops (
    operation_id text primary key,
    parent_type text not null check(parent_type in ('return_visit', 'communication', 'call', 'call_create')),
    parent_id text not null,
    attachment_id text not null,
    expected_revision integer not null,
    operation_type text not null check(operation_type in ('publish', 'delete')),
    status text not null check(status in ('staging_publish', 'pending_publish', 'publish_committed', 'staging_delete', 'delete_committed')),
    temp_rel_path text not null default '',
    final_rel_path text not null default '',
    tombstone_rel_path text not null default '',
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);
create unique index if not exists idx_attachment_file_ops_parent
on attachment_file_ops(parent_type, parent_id);

create table if not exists customer_hub_tasks (
    task_seq integer primary key autoincrement,
    task_id text not null unique,
    task_type text not null check(task_type in ('transcription', 'summary')),
    target_type text not null,
    target_id text not null,
    target_revision integer not null,
    actor_type text not null check(actor_type in ('human', 'service')),
    actor_user_id text not null default '',
    required_permission text not null default '',
    status text not null check(status in ('queued', 'running', 'succeeded', 'failed', 'cancelled_forbidden', 'cancelled_conflict')),
    error_code text not null default '',
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp
);

-- === 沟通记录(客户沟通→沟通咨询：电话/微信联系记录) ===
-- 与面诊(consults)各是各的：面诊=看牙病历记录，沟通记录=客户联系(电话咨询/微信沟通)。
-- 微信截图落 communication_images(仿 calls 落盘范式，模块自持不混入影像库 patient_images)。
create table if not exists communications (
    communication_id text primary key,
    patient_identity text not null default '',   -- 关联患者
    channel text not null default '',            -- phone 电话 / wechat 微信
    direction text not null default '',
    reason text not null default '',
    contacted_at text not null default '',       -- 沟通时间(北京时间)
    operator text not null default '',           -- 沟通人
    content text not null default '',            -- 沟通内容
    deal_status text not null default '',        -- 成交状态(未成交/已成交/意向/无效...)复用 calls 词表
    note text not null default '',
    revision integer not null default 1,
    created_at text not null default '',
    updated_at text not null default ''
);
create index if not exists idx_comm_patient on communications(patient_identity, contacted_at);

create table if not exists communication_images (
    image_id text primary key,
    communication_id text not null default '',   -- 所属沟通记录
    patient_identity text not null default '',
    rel_path text not null default '',           -- 截图文件相对 images_dir 路径
    file_name text not null default '',
    size_bytes integer not null default 0,
    content_hash text not null default '',
    created_at text not null default ''
);
create index if not exists idx_comm_images_comm on communication_images(communication_id);

-- === 回访微信截图（客户通→回访卡片：微信回访的截图上传，仿 communication_images 落盘范式）===
create table if not exists return_visit_images (
    image_id text primary key,
    return_visit_id text not null default '',   -- 所属回访
    patient_identity text not null default '',
    rel_path text not null default '',           -- 截图文件相对 images_dir 路径
    file_name text not null default '',
    size_bytes integer not null default 0,
    content_hash text not null default '',
    created_at text not null default ''
);
create index if not exists idx_rv_images_rv on return_visit_images(return_visit_id);

-- === CT/CBCT 云端检查档案 ===
-- 片子本体在云端影像服务商的服务器,本地只存"某患者某天拍了 CT"的索引(DICOM UID 三件套)。
-- 阅片链接带时效 token 不可久存,查看时由同步扩展用登录态现换。
create table if not exists patient_ct_studies (
    study_key text primary key,                  -- dataguid,缺失时退化为 studyuid:seriesuid:sopuid
    patient_identity text not null default '',
    modality text not null default '',           -- CT / CBCT
    filetype text not null default '',           -- SaaS 原始 filetype(换链接时要原样传回)
    studyuid text not null default '',
    seriesuid text not null default '',
    sopuid text not null default '',
    clinicid text not null default '',           -- 拍片诊所(imgclinicid)
    study_datetime text not null default '',     -- 拍摄时间
    upload_time text not null default '',        -- 上传云端时间
    created_at text not null default ''
);
create index if not exists idx_ct_studies_patient on patient_ct_studies(patient_identity);
