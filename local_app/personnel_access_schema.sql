-- === 人事/账号访问权限重构 · 目标 schema（冻结契约）===
-- 仅由后续"临时副本事务"读取执行；本文件只含建表 DDL，不含运行时指令或存在性判断，
-- 不承担静默兼容旧结构漂移的责任。

create table staff_members (
    staff_id text primary key not null check (trim(staff_id) <> ''),
    name text not null,
    note text not null default '',
    job_no text not null default '',
    phone text not null default '',
    sex text not null default '',
    id_card text not null default '',
    title text not null default '',
    license_no text not null default '',
    department text not null default '',
    employment_status text not null default 'employed'
        check (employment_status in ('employed', 'left', 'archived')),
    left_at text,
    left_reason text not null default '',
    created_at text,
    updated_at text
);

create table staff_role_assignments (
    staff_id text not null references staff_members(staff_id) check (trim(staff_id) <> ''),
    role_key text not null references roles(role_key) check (trim(role_key) <> ''),
    is_primary integer not null default 0 check (is_primary in (0, 1)),
    created_at text,
    primary key (staff_id, role_key)
);
create unique index idx_staff_role_assignments_one_primary
on staff_role_assignments(staff_id)
where is_primary = 1;
create index idx_staff_role_assignments_role
on staff_role_assignments(role_key);

create index idx_staff_members_employment_status
on staff_members(employment_status);

create table users (
    id text primary key not null check (trim(id) <> ''),
    username text not null unique check (trim(username) <> ''),
    display_name text not null default '',
    password_hash text not null check (trim(password_hash) <> ''),
    staff_id text references staff_members(staff_id)
        check (staff_id is null or trim(staff_id) <> ''),
    is_active integer not null default 1 check (is_active in (0, 1)),
    is_system_admin integer not null default 0 check (is_system_admin in (0, 1)),
    account_kind text not null default 'staff'
        check (account_kind in ('staff', 'independent_admin', 'break_glass')),
    created_at text not null default current_timestamp,
    updated_at text not null default current_timestamp,
    check (
        (account_kind = 'staff' and staff_id is not null)
        or
        (account_kind in ('independent_admin', 'break_glass')
            and staff_id is null and is_system_admin = 1)
    )
);
create unique index idx_users_staff_id_unique
on users(staff_id)
where staff_id is not null;

create table sessions (
    session_id text primary key not null check (trim(session_id) <> ''),
    token_hash text not null unique check (trim(token_hash) <> ''),
    user_id text not null references users(id) on delete cascade
        check (trim(user_id) <> ''),
    device_name text not null default '',
    user_agent text not null default '',
    ip_address text not null default '',
    created_at text not null,
    last_seen_at text not null,
    idle_expires_at text not null,
    reauthenticated_at text not null
);
create index idx_sessions_user on sessions(user_id);

create table role_data_scopes (
    role_key text not null references roles(role_key) check (trim(role_key) <> ''),
    scope_key text not null check (trim(scope_key) <> ''),
    scope_value text not null default '',
    primary key (role_key, scope_key)
);

create table audit_logs (
    audit_id integer primary key autoincrement,
    entity_type text not null,
    entity_id text not null,
    action text not null,
    old_json text,
    new_json text,
    operator text,
    created_at text not null default current_timestamp,
    actor_user_id text,
    result text check (result is null or result in ('success', 'denied', 'failed')),
    reason text,
    risk_level text,
    request_id text,
    ip_address text,
    user_agent text
);
create index idx_audit_logs_entity on audit_logs(entity_type, entity_id);
create index idx_audit_logs_created on audit_logs(created_at);
