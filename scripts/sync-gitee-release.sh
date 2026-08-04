#!/usr/bin/env bash
# 把 GitHub Release 的产物同步到 Gitee 发行版（国内网络本地跑，秒级完成）。
# 用法:
#   GITEE_TOKEN=xxx scripts/sync-gitee-release.sh v0.1.5
# 或先把令牌写进 ~/.gitee_token(权限 600),之后直接:
#   scripts/sync-gitee-release.sh v0.1.5
set -euo pipefail

TAG="${1:?用法: sync-gitee-release.sh <标签,如 v0.1.5>}"
GITEE_REPO="gubaoye/GD-DentOS"
GITHUB_REPO="GDguzi/GD-DentOS"
FILES=(good-dental-clinic-windows.zip good-dental-clinic-mac.zip)

TOKEN="${GITEE_TOKEN:-$(cat ~/.gitee_token 2>/dev/null || true)}"
[ -n "$TOKEN" ] || { echo "缺少令牌:设 GITEE_TOKEN 环境变量,或写入 ~/.gitee_token"; exit 1; }

API="https://gitee.com/api/v5/repos/${GITEE_REPO}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 从 GitHub 下载 ${TAG} 产物 =="
for n in "${FILES[@]}"; do
  [ -f "$TMP/$n" ] || curl -sfL -o "$TMP/$n" \
    "https://github.com/${GITHUB_REPO}/releases/download/${TAG}/${n}"
  echo "已下载: $n"
done

echo "== 查/建 Gitee 发行版 =="
id=$(curl -sf "${API}/releases/tags/${TAG}?access_token=${TOKEN}" \
     | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))" || true)
if [ -z "$id" ]; then
  id=$(curl -sf -X POST "${API}/releases" \
    -F "access_token=${TOKEN}" -F "tag_name=${TAG}" -F "name=${TAG}" \
    -F "body=Windows / macOS 一键版，下载即用。安装见 docs/产品手册.md。" \
    -F "target_commitish=main" \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
  echo "已建 Gitee 发行版: $TAG (id=$id)"
else
  echo "Gitee 发行版已存在: $TAG (id=$id)"
fi

have=$(curl -sf "${API}/releases/${id}/attach_files?access_token=${TOKEN}" \
       | python3 -c "import json,sys;print(' '.join(a['name'] for a in json.load(sys.stdin)))")

echo "== 上传附件(已存在则跳过) =="
for n in "${FILES[@]}"; do
  case " ${have} " in
    *" ${n} "*) echo "已有,跳过: $n"; continue;;
  esac
  curl -sf -X POST "${API}/releases/${id}/attach_files" \
    -F "access_token=${TOKEN}" -F "file=@${TMP}/${n}" -o /dev/null
  echo "已上传: $n"
done

echo "完成 → https://gitee.com/${GITEE_REPO}/releases/${TAG}"
